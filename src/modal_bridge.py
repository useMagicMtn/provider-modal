"""Private adapter embedded in the Rust provider; stdout carries one JSON result."""
import asyncio
import json
import math
import os
import re
import shlex
import sys
import time

import modal
from modal.exception import ExecTimeoutError


class ConfigurationError(ValueError):
    """Safe operator guidance; never includes configuration values."""


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
        client=client, create_if_missing=client is None,
    )
    expected = os.environ.get('MAGICMTN_MODAL_APP_ID')
    if expected is not None and app.app_id != expected:
        raise ConfigurationError('Modal app ID differs from the configured account')
    return app


async def dispatch(request, client=None):
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
    # Keep cloud control calls within the harness's existing creation deadline.
    async with asyncio.timeout(45):
        request, credential = json.load(sys.stdin)
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
    print(json.dumps(result, separators=(',', ':')))
