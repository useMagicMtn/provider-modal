"""Embedded Modal adapter. Stdout carries one JSON result."""
import asyncio
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
from modal.exception import ExecTimeoutError


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
    )
    expected = os.environ.get('MAGICMTN_MODAL_APP_ID')
    if expected is not None and app.app_id != expected:
        raise ConfigurationError('Modal app ID differs from the configured account')
    return app


async def dispatch(request, client=None):
    if request['operation'] == 'configure':
        if client is None:
            raise ConfigurationError('Modal credentials are required for configuration')
        options = request.get('options', {})
        if not isinstance(options, dict) or set(options) - {'app', 'inference_url', 'inference_secret_file'}:
            raise ConfigurationError('Unknown Modal setup options')
        endpoint = options.get('inference_url', os.environ.get('MAGICMTN_MODAL_INFERENCE_URL'))
        if endpoint is not None:
            endpoint = inference_url(endpoint)
        secret_file = options.get('inference_secret_file', os.environ.get('MAGICMTN_MODAL_INFERENCE_SECRET_FILE'))
        if secret_file is not None and (not isinstance(secret_file, str) or not os.path.isabs(secret_file) or '\0' in secret_file):
            raise ConfigurationError('Modal inference secret file must use an absolute path')
        name = options.get('app', os.environ.get('MAGICMTN_MODAL_APP', 'magicmtn'))
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
            raise ConfigurationError('Invalid Modal app name')
        expected = os.environ.get('MAGICMTN_MODAL_APP_ID')
        # Rotation and reconfiguration must not create replacement resources.
        app = await modal.App.lookup.aio(name, client=client, create_if_missing=expected is None)
        if expected is not None and app.app_id != expected:
            raise ConfigurationError('Modal app ID differs from the configured account')
        configuration = {'MAGICMTN_MODAL_APP': name, 'MAGICMTN_MODAL_APP_ID': app.app_id}
        if endpoint is not None:
            configuration['MAGICMTN_MODAL_INFERENCE_URL'] = endpoint
        if secret_file is not None:
            configuration['MAGICMTN_MODAL_INFERENCE_SECRET_FILE'] = secret_file
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
    async with asyncio.timeout(1600 if request.get('operation') == 'prepare_image' else 45):
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
