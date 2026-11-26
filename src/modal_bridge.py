"""Embedded Modal adapter. Stdout carries one JSON result."""
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import stat
import tempfile
import uuid
import json
import math
import os
import re
import shlex
import sys
import time
import urllib.request
from urllib.parse import urlsplit

import modal
from modal.exception import ExecTimeoutError, NotFoundError, ConflictError, InvalidError, AuthError, PermissionDeniedError
from modal_proto import api_pb2


class ConfigurationError(ValueError):
    """Safe operator guidance; never includes configuration values."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Proxy Tokens to a redirect destination.
        return None


def inference_url(endpoint):
    if not isinstance(endpoint, str):
        raise ConfigurationError('Modal inference requires an HTTPS API base URL')
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in endpoint)):
        raise ConfigurationError('Modal inference requires an HTTPS API base URL without credentials, query, or fragment')
    return endpoint.rstrip('/')


def inference(request, credential):
    endpoint = inference_url(request['endpoint'])
    if not credential or any(not credential.get(key) for key in ('MODAL_PROXY_TOKEN_ID', 'MODAL_PROXY_TOKEN_SECRET')):
        raise ConfigurationError('Modal inference requires Proxy Token credentials')
    token = credential['MODAL_PROXY_TOKEN_ID'] + '.' + credential['MODAL_PROXY_TOKEN_SECRET']
    if any(c.isspace() or ord(c) < 32 for c in token):
        raise ConfigurationError('Invalid Modal Proxy Token credentials')
    body = json.dumps(request['body'], allow_nan=False).encode()
    call = urllib.request.Request(endpoint.rstrip('/') + '/chat/completions', data=body,
                                  headers={'Authorization': 'Bearer ' + token,
                                           'Content-Type': 'application/json', 'Accept': 'application/json'})
    # One attempt only: a failed response can still represent completed inference.
    with urllib.request.build_opener(NoRedirect()).open(call, timeout=290) as response:
        result = response.read(2 * 1024 * 1024 + 1)
    if len(result) > 2 * 1024 * 1024:
        raise ConfigurationError('Modal inference response exceeds 2 MiB')
    return json.loads(result)


def sandbox_command(command):
    # Modal does not honor OCI USER. Quote every argument before the su shell.
    return ['/bin/su', '-s', '/bin/sh', '-c', 'exec ' + shlex.join(command), 'sandbox']


def prepared_image(image, client):
    if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', image):
        raise ConfigurationError('Modal requires an OCI image pinned by SHA-256 digest')
    try:
        mapping = json.loads(os.environ.get('MAGICMTN_MODAL_PREPARED_IMAGES', '{}'))
        image_id = mapping.get(image) if isinstance(mapping, dict) else None
    except (ValueError, TypeError):
        image_id = None
    if not isinstance(image_id, str) or not re.fullmatch(r'im-[A-Za-z0-9]+', image_id):
        raise ConfigurationError('Configure MAGICMTN_MODAL_PREPARED_IMAGES with the verified OCI digest mapped to its prepared Modal image ID')
    # A pinned object ID avoids mutable named-image aliases and inline builds.
    return modal.Image.from_id(image_id, client=client), image_id


async def capture(stream, limit):
    output = bytearray()
    truncated = False
    try:
        async for chunk in stream:
            keep = min(len(chunk), max(0, limit - len(output)))
            output.extend(chunk[:keep])
            truncated |= keep < len(chunk)
    except ExecTimeoutError:
        return output, truncated, True
    return output, truncated, False


async def execute(sandbox, request):
    started = time.monotonic()
    limit = request['max_output_bytes']
    process = await sandbox.exec.aio(
        *sandbox_command([request['command'], *request['args']]),
        timeout=math.ceil(request['timeout_ms'] / 1000), text=False, workdir='/workspace',
    )
    # Drain both streams concurrently, retaining only the requested number of bytes.
    out = asyncio.create_task(capture(process.stdout, limit))
    err = asyncio.create_task(capture(process.stderr, limit))
    wait = asyncio.create_task(process.wait.aio())
    timed_out = False
    code = None
    try:
        code = await asyncio.wait_for(asyncio.shield(wait), request['timeout_ms'] / 1000)
        stdout, stderr = await asyncio.wait_for(asyncio.gather(out, err), 5)
        timed_out = code == -1 or stdout[2] or stderr[2]
    except (TimeoutError, ExecTimeoutError):
        timed_out = True
        stdout = out.result() if out.done() and not out.cancelled() and out.exception() is None else (b'', False, True)
        stderr = err.result() if err.done() and not err.cancelled() and err.exception() is None else (b'', False, True)
    finally:
        for task in (out, err, wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(out, err, wait, return_exceptions=True)
    # Replacement characters can expand malformed UTF-8 beyond the byte budget.
    def text(result):
        encoded = bytes(result[0]).decode('utf-8', errors='replace').encode('utf-8')
        return encoded[:limit].decode('utf-8', errors='ignore'), result[1] or len(encoded) > limit
    stdout, out_truncated = text(stdout)
    stderr, err_truncated = text(stderr)
    return {
        'stdout': stdout,
        'stderr': stderr,
        'exit_code': None if timed_out else code,
        'elapsed_ms': int((time.monotonic() - started) * 1000),
        'timed_out': timed_out,
        'truncated': out_truncated or err_truncated,
    }


async def lookup_app(client):
    app = await modal.App.lookup.aio(
        os.environ.get('MAGICMTN_MODAL_APP', 'magicmtn'),
        client=client, create_if_missing=False,
        **({'environment_name': os.environ['MAGICMTN_MODAL_ENVIRONMENT']} if os.environ.get('MAGICMTN_MODAL_ENVIRONMENT') else {}),
    )
    expected = os.environ.get('MAGICMTN_MODAL_APP_ID')
    if expected is not None and app.app_id != expected:
        raise ConfigurationError('Modal app ID differs from the configured account')
    return app


_sdk_rpc = None


async def _rpc_on_sdk_loop(client, method, request):
    return await getattr(client._stub, method)(request)


async def modal_rpc(client, method, request):
    # Public Modal clients run their transport on the SDK synchronizer's loop.
    # Translate the client and execute private endpoint RPCs on that same loop.
    global _sdk_rpc
    if _sdk_rpc is None:
        from modal._utils.async_utils import synchronizer
        _sdk_rpc = synchronizer.create_blocking(_rpc_on_sdk_loop)
    return await _sdk_rpc.aio(client, method, request)


@contextmanager
def managed_state(request, app_id, create=False):
    """Serialize explicit reconciliation and retain receipts before mutations."""
    directory = request.get('state_directory') or os.environ.get('MAGICMTN_MODAL_STATE_DIRECTORY')
    if not isinstance(directory, str) or not os.path.isabs(directory):
        raise ConfigurationError('A private persistent state_directory is required for managed Modal endpoints')
    directory = Path(directory)
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ConfigurationError('Modal state_directory must be a private directory owned by the provider host user')
    descriptor = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if create or request['operation'] == 'endpoint_delete' else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError('Modal endpoint reconciliation is already running') from None
        state_path = directory / 'deployments.json'
        if state_path.exists():
            state = private_json(state_path)
            if state.get('schema') != 1 or state.get('app_id') != app_id:
                raise ConfigurationError('Modal managed state belongs to a different account')
        elif create:
            state = {'schema': 1, 'app_id': app_id, 'installation': uuid.uuid4().hex, 'deployments': {}}
            atomic_json(state_path, state)
        else:
            raise ConfigurationError('Modal managed endpoint state is missing')
        yield directory, state


def private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ConfigurationError('Modal managed state and credential files must be private')
        data = file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ConfigurationError('Modal managed state exceeds its size limit')
    return json.loads(data)


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as file:
            json.dump(value, file, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def endpoint_configuration(spec):
    if (not isinstance(spec, dict) or not isinstance(spec.get('name'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', spec['name'])
            or not isinstance(spec.get('model'), str)
            or not re.fullmatch(r'[A-Za-z0-9/_.-]{1,256}', spec['model'])):
        raise ConfigurationError('Invalid Modal deployment specification')
    configuration = spec.get('configuration')
    if configuration is None:
        configuration = {}
    if not isinstance(configuration, dict) or set(configuration) - {'serving_mode', 'routing_region'}:
        raise ConfigurationError('Unknown Modal endpoint configuration')
    mode = configuration.get('serving_mode', 'shared')
    region = configuration.get('routing_region', 'us-west')
    if mode not in ('shared', 'dedicated') or region not in ('us-west', 'us-east', 'eu-west'):
        raise ConfigurationError('Invalid Modal endpoint serving mode or routing region')
    return {'model': spec['model'], 'serving_mode': mode, 'routing_region': region}


def owned_endpoint(state, endpoint_id):
    if not isinstance(endpoint_id, str) or not re.fullmatch(r'ep-[A-Za-z0-9]{22}', endpoint_id):
        raise ConfigurationError('Invalid Modal endpoint ID')
    matches = [record for record in state['deployments'].values() if record.get('endpoint_id') == endpoint_id]
    if len(matches) != 1:
        raise ConfigurationError('Modal endpoint is not managed by this provider connection')
    return matches[0]


async def endpoint_info(client, endpoint_id, record):
    response = await modal_rpc(client, 'EndpointGetInfo', api_pb2.EndpointGetInfoRequest(endpoint_id=endpoint_id))
    info = response.info
    mode = api_pb2.ENDPOINT_SERVING_MODE_SHARED if record['spec']['serving_mode'] == 'shared' else api_pb2.ENDPOINT_SERVING_MODE_DEDICATED
    if (info.name != record['name'] or info.repo_id != record['spec']['model']
            or info.serving_mode != mode or not info.requires_proxy_auth):
        raise ConfigurationError('Modal endpoint identity or serving configuration changed')
    expected_environment = record.get('environment')
    if expected_environment and info.lifecycle.environment_name != expected_environment:
        raise ConfigurationError('Modal endpoint environment changed')
    return info


def endpoint_snapshot(endpoint_id, record, info=None, deleted=False):
    if deleted or (info is not None and info.lifecycle.status == api_pb2.ENDPOINT_LIFECYCLE_STATUS_STOPPED):
        state = 'deleted'
    elif info is None:
        state = 'creating'
    else:
        state = {
            api_pb2.EndpointGetInfoResponse.ENDPOINT_STATUS_PROVISIONING: 'creating',
            api_pb2.EndpointGetInfoResponse.ENDPOINT_STATUS_LIVE: 'ready',
            api_pb2.EndpointGetInfoResponse.ENDPOINT_STATUS_FAILED: 'failed',
            api_pb2.EndpointGetInfoResponse.ENDPOINT_STATUS_CANCELLING: 'deleting',
            api_pb2.EndpointGetInfoResponse.ENDPOINT_STATUS_CANCELLED: 'failed',
            api_pb2.EndpointGetInfoResponse.ENDPOINT_STATUS_STOPPED: 'deleted',
        }.get(info.status, 'unknown')
    return {'resource': {'provider_id': endpoint_id}, 'model': record['spec']['model'],
            'inference_model': 'endpoint:' + endpoint_id, 'state': state,
            'ready_replicas': None}


async def create_endpoint(client, directory, state, spec):
    desired = endpoint_configuration(spec)
    record = state['deployments'].get(spec['name'])
    if record is not None and record['spec'] != desired:
        raise ConfigurationError('Modal deployment configuration changed; use a new resource name')
    if record is None:
        name = 'mm-' + state['installation'][:12] + '-' + hashlib.sha256(spec['name'].encode()).hexdigest()[:16]
        record = {'name': name, 'spec': desired, 'phase': 'new', 'environment': state['environment']}
        state['deployments'][spec['name']] = record
        atomic_json(directory / 'deployments.json', state)
    if record.get('endpoint_id'):
        return record['endpoint_id'], record
    try:
        response = await modal_rpc(client, 'EndpointGetByName', api_pb2.EndpointGetByNameRequest(
            name=record['name'], environment_name=state.get('environment', '')))
        existing_id = response.endpoint_id
    except NotFoundError:
        existing_id = None
    if existing_id:
        if record['phase'] != 'creating':
            raise ConfigurationError('Modal endpoint name is already in use')
        endpoint_id = existing_id
        await endpoint_info(client, endpoint_id, record)
    else:
        if record['phase'] == 'creating':
            raise ConfigurationError('Modal endpoint create outcome is uncertain; reconcile its name before creating another resource')
        record['phase'] = 'creating'
        atomic_json(directory / 'deployments.json', state)
        compute = api_pb2.EndpointComputeRegionSpec()
        compute.auto.SetInParent()
        try:
            response = await modal_rpc(client, 'EndpointCreate', api_pb2.EndpointCreateRequest(
                name=record['name'], description='Managed by MagicMtn; connection ' + state['app_id'],
                model=api_pb2.EndpointModelSource(base_model_repo_id=desired['model']),
                environment_name=state.get('environment', ''),
                proxy_regions=[desired['routing_region']], compute_region=compute,
                serving_mode=(api_pb2.ENDPOINT_SERVING_MODE_SHARED if desired['serving_mode'] == 'shared'
                              else api_pb2.ENDPOINT_SERVING_MODE_DEDICATED),
                shared_endpoint_notice_acknowledged=desired['serving_mode'] == 'shared',
                unauthenticated=False,
            ))
        except (ConflictError, InvalidError, AuthError, PermissionDeniedError):
            record['phase'] = 'new'
            atomic_json(directory / 'deployments.json', state)
            raise
        endpoint_id = response.endpoint_id
    if not re.fullmatch(r'ep-[A-Za-z0-9]{22}', endpoint_id):
        raise ConfigurationError('Modal returned an invalid endpoint ID')
    record.update(endpoint_id=endpoint_id, phase='created')
    atomic_json(directory / 'deployments.json', state)
    return endpoint_id, record


async def ensure_proxy_token(client, directory, state):
    environment_name = state.get('environment') or os.environ.get('MAGICMTN_MODAL_ENVIRONMENT')
    environment = await (modal.Environment.from_name(environment_name, client=client) if environment_name
                         else modal.Environment.from_context(client=client)).hydrate.aio()
    if not environment.name:
        raise ConfigurationError('Modal did not return the endpoint environment')
    state['environment'] = environment.name
    atomic_json(directory / 'deployments.json', state)
    token_path = directory / 'proxy.json'
    if token_path.exists():
        credential = private_json(token_path)
    else:
        if state.get('token_pending'):
            raise ConfigurationError('Modal Proxy Token creation outcome is uncertain; reconcile the recorded token name before retrying')
        state['token_name'] = 'magicmtn-' + state['installation']
        state['token_pending'] = True
        atomic_json(directory / 'deployments.json', state)
        try:
            token_request = api_pb2.WebhookTokenCreateRequest(name=state['token_name'], scoped=True)
            token = await modal_rpc(client, 'WebhookTokenCreate', token_request)
        except (ConflictError, InvalidError, AuthError, PermissionDeniedError):
            state['token_pending'] = False
            atomic_json(directory / 'deployments.json', state)
            raise
        credential = {'MODAL_PROXY_TOKEN_ID': token.token_id, 'MODAL_PROXY_TOKEN_SECRET': token.token_secret}
        if not re.fullmatch(r'wk-[A-Za-z0-9]+', token.token_id) or not re.fullmatch(r'ws-[A-Za-z0-9]+', token.token_secret):
            raise ConfigurationError('Modal returned invalid Proxy Token credentials')
        atomic_json(token_path, credential)
    await modal_rpc(client, 'WebhookTokenEnvironmentAdd', api_pb2.WebhookTokenEnvironmentAddRequest(
        token_id=credential['MODAL_PROXY_TOKEN_ID'], environment_id=environment.object_id))
    state['token_pending'] = False
    atomic_json(directory / 'deployments.json', state)
    return token_path


async def managed_operation(request, client, app_id=None):
    if app_id is None:
        app_id = (await lookup_app(client)).app_id
    operation = request['operation']
    with managed_state(request, app_id, create=operation == 'endpoint_create') as (directory, state):
        if operation == 'endpoint_create':
            endpoint_configuration(request['spec'])
            await ensure_proxy_token(client, directory, state)
            endpoint_id, record = await create_endpoint(client, directory, state, request['spec'])
            info = await endpoint_info(client, endpoint_id, record)
            record['environment'] = info.lifecycle.environment_name
            atomic_json(directory / 'deployments.json', state)
            return endpoint_snapshot(endpoint_id, record, info)
        endpoint_id = request['endpoint_id']
        record = owned_endpoint(state, endpoint_id)
        try:
            info = await endpoint_info(client, endpoint_id, record)
        except NotFoundError:
            lifecycle = await modal_rpc(client, 'EndpointGetLifecycle', api_pb2.EndpointGetLifecycleRequest(endpoint_id=endpoint_id))
            if lifecycle.lifecycle.status == api_pb2.ENDPOINT_LIFECYCLE_STATUS_STOPPED:
                if operation == 'infer_endpoint':
                    raise ConfigurationError('Modal endpoint has been deleted')
                return None if operation == 'endpoint_delete' else endpoint_snapshot(endpoint_id, record, deleted=True)
            raise ConfigurationError('Modal endpoint status cannot be established') from None
        snapshot = endpoint_snapshot(endpoint_id, record, info)
        if operation == 'endpoint_status':
            return snapshot
        if operation == 'endpoint_delete':
            if snapshot['state'] != 'deleted':
                await modal_rpc(client, 'EndpointStop', api_pb2.EndpointStopRequest(
                    endpoint_id=endpoint_id, source=api_pb2.ENDPOINT_STOP_SOURCE_CLI))
            return None
        if operation == 'infer_endpoint':
            if snapshot['state'] != 'ready':
                raise ConfigurationError('Modal endpoint is not ready for inference; inspect its deployment status')
            endpoint = inference_url(info.service_url)
            hostname = urlsplit(endpoint).hostname
            if not hostname.endswith(('.modal.direct', '.modal.run')):
                raise ConfigurationError('Modal returned an invalid endpoint hostname')
            credential = private_json(directory / 'proxy.json')
            body = dict(request['body'], model=info.name)
            http_request = {'endpoint': endpoint.rstrip('/') + '/v1', 'body': body}
        else:
            raise ConfigurationError('Unsupported managed Modal endpoint operation')
    # Release the state lock before model execution, allowing concurrent calls.
    return await asyncio.to_thread(inference, http_request, credential)



async def dispatch(request, client=None):
    if request['operation'] in ('endpoint_create', 'endpoint_status', 'endpoint_delete', 'infer_endpoint'):
        if client is None:
            raise ConfigurationError('Modal workspace credentials are required')
        return await managed_operation(request, client)
    if request['operation'] == 'configure':
        if client is None:
            raise ConfigurationError('Modal credentials are required for configuration')
        options = request.get('options', {})
        if not isinstance(options, dict) or set(options) - {'app', 'inference_url', 'inference_secret_file', 'state_directory'}:
            raise ConfigurationError('Unknown Modal setup options')
        endpoint = options.get('inference_url', os.environ.get('MAGICMTN_MODAL_INFERENCE_URL')) or None
        if endpoint is not None:
            endpoint = inference_url(endpoint)
        secret_file = options.get('inference_secret_file', os.environ.get('MAGICMTN_MODAL_INFERENCE_SECRET_FILE'))
        if secret_file is not None and (not isinstance(secret_file, str) or not os.path.isabs(secret_file) or '\0' in secret_file):
            raise ConfigurationError('Modal inference secret file must use an absolute path')
        directory = options.get('state_directory', request.get('state_directory') or os.environ.get('MAGICMTN_MODAL_STATE_DIRECTORY'))
        if directory is not None:
            if not isinstance(directory, str) or not os.path.isabs(directory) or '\0' in directory:
                raise ConfigurationError('Modal state_directory must use an absolute path')
            saved_directory = os.environ.get('MAGICMTN_MODAL_STATE_DIRECTORY')
            if saved_directory and os.path.realpath(directory) != os.path.realpath(saved_directory):
                raise ConfigurationError('Modal state directory differs from the configured connection')
        name = options.get('app', os.environ.get('MAGICMTN_MODAL_APP', 'magicmtn'))
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
            raise ConfigurationError('Invalid Modal app name')
        expected = os.environ.get('MAGICMTN_MODAL_APP_ID')
        # Rotation and reconfiguration must not create replacement resources.
        app = await modal.App.lookup.aio(name, client=client, create_if_missing=expected is None,
            **({'environment_name': os.environ['MAGICMTN_MODAL_ENVIRONMENT']} if os.environ.get('MAGICMTN_MODAL_ENVIRONMENT') else {}))
        if expected is not None and app.app_id != expected:
            raise ConfigurationError('Modal app ID differs from the configured account')
        configuration = {'MAGICMTN_MODAL_APP': name, 'MAGICMTN_MODAL_APP_ID': app.app_id}
        if endpoint is not None:
            configuration['MAGICMTN_MODAL_INFERENCE_URL'] = endpoint
        if secret_file is not None:
            configuration['MAGICMTN_MODAL_INFERENCE_SECRET_FILE'] = secret_file
        if directory is not None:
            configuration['MAGICMTN_MODAL_STATE_DIRECTORY'] = directory
        return {'account': app.app_id, 'configuration': configuration}
    if request['operation'] == 'prepare_image':
        reference = request['image']
        if not re.fullmatch(r'[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}', reference):
            raise ConfigurationError('Image preparation requires an immutable OCI digest')
        # Use the builder supported by this SDK instead of legacy account defaults.
        os.environ['MODAL_IMAGE_BUILDER_VERSION'] = '2025.06'
        app = await lookup_app(client)
        image = modal.Image.from_registry(reference)
        await image.build.aio(app=app)
        sandbox = await modal.Sandbox.create.aio(
            *sandbox_command(['/bin/sh', '-c', 'while true; do sleep 3600; done']),
            app=app, image=image, client=client, runtime='vm', workdir='/workspace',
            timeout=120, cpu=(1.0, 1.0), memory=(512, 512),
            tags={'magicmtn': 'modal-image-preparation', 'magicmtn_image': reference},
        )
        try:
            probe = await sandbox.exec.aio(*sandbox_command(['python3', '-c',
                "import os,shutil,subprocess;assert os.getuid()==10001 and os.getgid()==10001;assert os.access('/workspace',os.W_OK);assert os.access('/usr/local/bin/magicmtn-sandbox-agent',os.X_OK);assert shutil.which('pi');subprocess.run(['pi','--version'],check=True,stdout=subprocess.DEVNULL)"]),
                timeout=30, workdir='/workspace')
            if await probe.wait.aio() != 0:
                raise ConfigurationError('Image must include the MagicMtn sandbox runtime, Pi, and a writable /workspace for sandbox user 10001')
        finally:
            await sandbox.terminate.aio()
        repository, digest = reference.rsplit('@', 1)
        return {'image': {'repository': repository, 'digest': digest},
                'platform': 'linux/amd64', 'artifact': image.object_id,
                'configuration': {'MAGICMTN_MODAL_PREPARED_IMAGES': json.dumps({reference: image.object_id})}}
    if request['operation'] == 'verify_account':
        if client is None:
            raise ConfigurationError('Modal credentials are required for account verification')
        return (await lookup_app(client)).app_id
    if request['operation'] == 'create':
        ttl_seconds = request.get('ttl_seconds', 3600)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ConfigurationError('Modal sandbox TTL must be 1 to 86400 seconds')
        image, image_id = prepared_image(request['image'], client)
        app = await lookup_app(client)
        sandbox = await modal.Sandbox.create.aio(
            *sandbox_command(['/bin/sh', '-c', 'while true; do sleep 3600; done']),
            app=app, image=image, client=client, name='magicmtn-session-' + request['session'],
            tags={'magicmtn': 'modal', 'magicmtn_image': request['image'], 'magicmtn_session': request['session'],
                  'magicmtn_runtime': 'vm', 'magicmtn_user': 'sandbox', 'magicmtn_prepared_image': image_id},
            cpu=(1.0, 1.0), memory=(512, 512), timeout=ttl_seconds,
            block_network=False, runtime='vm', workdir='/workspace',
        )
        try:
            probe = await sandbox.exec.aio(*sandbox_command(['python3', '-c',
                "import os;assert os.getuid()==10001 and os.getgid()==10001;assert os.access('/workspace',os.W_OK);assert os.access('/usr/local/bin/magicmtn-sandbox-agent',os.X_OK)"]), timeout=5, workdir='/workspace')
            if await probe.wait.aio() != 0:
                raise RuntimeError('sandbox readiness check failed')
        except BaseException:
            await sandbox.terminate.aio()
            raise
        return {
            'provider_id': sandbox.object_id, 'status': 'running',
            'create_time_ms': int(time.time() * 1000),
            'metadata': {'provider': 'modal', 'image': request['image'], 'isolated': True,
                         'runtime': 'vm', 'user': 'sandbox', 'prepared_image': image_id},
        }
    record = request['sandbox']
    sandbox = await modal.Sandbox.from_id.aio(record['provider_id'], client=client)
    tags = await sandbox.get_tags.aio()
    if tags.get('magicmtn') != 'modal' or not tags.get('magicmtn_session'):
        raise RuntimeError('sandbox is not managed by this provider')
    image = (record.get('metadata') or {}).get('image')
    if image is not None and tags.get('magicmtn_image') != image:
        raise RuntimeError('sandbox image changed')
    if request['operation'] == 'terminate':
        await sandbox.terminate.aio()
        return None
    if request['operation'] == 'exec':
        metadata = record.get('metadata') or {}
        if (tags.get('magicmtn_runtime') != 'vm' or tags.get('magicmtn_user') != 'sandbox'
                or not tags.get('magicmtn_prepared_image')
                or any(metadata.get(key) != tags.get('magicmtn_' + key)
                       for key in ('runtime', 'user', 'prepared_image'))):
            raise ConfigurationError('Sandbox predates the verified VM/user contract; terminate it and start a new session')
        if await sandbox.poll.aio() is not None:
            raise RuntimeError('sandbox is no longer running')
        return await execute(sandbox, request)
    raise ValueError('unsupported operation')


async def main():
    # Keep Modal API calls within the provider's request deadline.
    request, credential = json.load(sys.stdin)
    if request.get('operation') == 'infer':
        # Inference uses Proxy Tokens and does not open a workspace SDK client.
        return await asyncio.to_thread(inference, request, credential)
    async with asyncio.timeout(1600 if request.get('operation') == 'prepare_image'
                               else 300 if request.get('operation') == 'infer_endpoint' else 45):
        if credential is None:
            return await dispatch(request)
        client = await modal.Client.from_credentials.aio(
            credential['MODAL_TOKEN_ID'], credential['MODAL_TOKEN_SECRET'],
        )
        # The opened SDK client registers cleanup with asyncio's shutdown.
        return await dispatch(request, client)


if __name__ == '__main__':
    try:
        result = asyncio.run(main())
    except Exception as error:
        # Exception text can include credentials or request details; return only its type.
        result = {'error': 'Modal operation failed (' + type(error).__name__ + ')'}
        if isinstance(error, ConfigurationError):
            result = {'error': str(error)}
    print(json.dumps(result, separators=(',', ':'), ensure_ascii=False))
