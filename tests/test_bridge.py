import asyncio
import json
import shlex
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Load the native protobuf module before patch.dict snapshots sys.modules.
# Evicting and reimporting it while live messages exist can crash Python 3.13.
from modal_proto import api_pb2


class AuthError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ExecTimeoutError(Exception):
    pass


async def stream(*chunks):
    for chunk in chunks:
        yield chunk


def process(code=0, stdout=(), stderr=()):
    return SimpleNamespace(stdout=stream(*stdout), stderr=stream(*stderr),
                           wait=SimpleNamespace(aio=AsyncMock(return_value=code)))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.modal = SimpleNamespace(
            App=SimpleNamespace(lookup=SimpleNamespace(aio=AsyncMock(return_value=SimpleNamespace(app_id='ap-TestApp')))),
            Image=SimpleNamespace(from_id=Mock(return_value='prepared-image')),
            Sandbox=SimpleNamespace(create=SimpleNamespace(aio=AsyncMock()),
                                    from_id=SimpleNamespace(aio=AsyncMock())))
        spec = importlib.util.spec_from_file_location('bridge', Path(__file__).parents[1] / 'src/modal_bridge.py')
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'modal': self.modal, 'modal.exception': SimpleNamespace(ExecTimeoutError=ExecTimeoutError, NotFoundError=NotFoundError, ConflictError=ConflictError, InvalidError=InvalidError, AuthError=AuthError, PermissionDeniedError=PermissionDeniedError)}):
            spec.loader.exec_module(self.bridge)
        async def modal_rpc(client, method, request):
            return await getattr(client._stub, method)(request)
        self.bridge.modal_rpc = modal_rpc
        self.image = 'registry.example/sandbox@sha256:' + 'a' * 64
        self.mapping = {'MAGICMTN_MODAL_PREPARED_IMAGES': json.dumps({self.image: 'im-verified'})}
        self.tags = {'magicmtn':'modal','magicmtn_session':'session','magicmtn_image':self.image, 'magicmtn_runtime':'vm','magicmtn_user':'sandbox','magicmtn_prepared_image':'im-verified'}
        self.sandbox = SimpleNamespace(
            object_id='sb-test', exec=SimpleNamespace(aio=AsyncMock(return_value=process())),
            terminate=SimpleNamespace(aio=AsyncMock()), poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
            get_tags=SimpleNamespace(aio=AsyncMock(return_value=self.tags)))
        self.modal.Sandbox.create.aio.return_value = self.sandbox
        self.modal.Sandbox.from_id.aio.return_value = self.sandbox
        self.request = {'operation':'exec','sandbox':{'provider_id':'sb-test','metadata':{'image':self.image, 'runtime':'vm','user':'sandbox','prepared_image':'im-verified'}},
                        'command':'echo','args':['literal; $value'], 'timeout_ms':1000,'max_output_bytes':4}

    def managed_client(self):
        api = api_pb2
        self.api = api
        self.endpoint_id = 'ep-' + 'A' * 22
        self.remote = {'name': '', 'model': '', 'status': api.EndpointGetInfoResponse.ENDPOINT_STATUS_LIVE,
                       'lifecycle': api.ENDPOINT_LIFECYCLE_STATUS_ACTIVE}
        async def create(request):
            self.remote.update(name=request.name, model=request.model.base_model_repo_id)
            return api.EndpointCreateResponse(endpoint_id=self.endpoint_id, name=request.name)
        async def info(request):
            self.assertEqual(request.endpoint_id, self.endpoint_id)
            return api.EndpointGetInfoResponse(info=api.EndpointGetInfoResponse.EndpointInfoSummary(
                name=self.remote['name'], repo_id=self.remote['model'], service_url='https://fixture.us-west.modal.direct',
                requires_proxy_auth=True, serving_mode=api.ENDPOINT_SERVING_MODE_SHARED,
                status=self.remote['status'], lifecycle=api.EndpointLifecycle(
                    status=self.remote['lifecycle'], environment_name='main')))
        stub = SimpleNamespace(
            EndpointGetByName=AsyncMock(return_value=api.EndpointGetByNameResponse()),
            EndpointCreate=AsyncMock(side_effect=create), EndpointGetInfo=AsyncMock(side_effect=info),
            EndpointGetLifecycle=AsyncMock(return_value=api.EndpointGetLifecycleResponse(
                lifecycle=api.EndpointLifecycle(status=api.ENDPOINT_LIFECYCLE_STATUS_STOPPED, environment_name='main'))),
            EndpointStop=AsyncMock(),
            WebhookTokenCreate=AsyncMock(return_value=SimpleNamespace(token_id='wk-Fixture', token_secret='ws-Fixture')),
            WebhookTokenEnvironmentAdd=AsyncMock(),
        )
        environment = SimpleNamespace(object_id='en-Fixture', name='main')
        self.modal.Environment = SimpleNamespace(from_name=Mock(return_value=SimpleNamespace(
            hydrate=SimpleNamespace(aio=AsyncMock(return_value=environment)))))
        self.modal.Environment.from_context = self.modal.Environment.from_name
        return SimpleNamespace(_stub=stub)

    def managed_request(self, directory):
        return {'operation': 'endpoint_create', 'state_directory': directory,
                'spec': {'name': 'example', 'model': 'acme/ChosenModel', 'configuration': {}}}

    async def test_account_configuration_does_not_provision_models_or_credentials(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            request = {'operation': 'configure', 'options': {'state_directory': directory}}
            first = await self.bridge.dispatch(request, client)
            self.assertEqual(first['configuration'], {'MAGICMTN_MODAL_APP': 'magicmtn',
                'MAGICMTN_MODAL_APP_ID': 'ap-TestApp', 'MAGICMTN_MODAL_STATE_DIRECTORY': directory})
            # A saved model must not make credential rotation provision anything.
            with patch.dict('os.environ', {**first['configuration'], 'MAGICMTN_MODAL_MODEL': 'legacy/model'}, clear=True):
                second = await self.bridge.dispatch({'operation': 'configure', 'options': {}}, client)
            self.assertEqual(first, second)
            self.assertEqual(list(Path(directory).iterdir()), [])
            for method in vars(client._stub).values():
                method.assert_not_awaited()
            self.modal.Environment.from_name.assert_not_called()
            self.modal.Sandbox.create.aio.assert_not_awaited()

    async def test_managed_creation_uses_requested_model_and_reuses_private_credentials(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            request = self.managed_request(directory)
            first = await self.bridge.dispatch(request, client)
            self.assertEqual(first['resource']['provider_id'], self.endpoint_id)
            self.assertEqual(first['model'], 'acme/ChosenModel')
            self.assertNotIn('ws-Fixture', json.dumps(first))
            create = client._stub.EndpointCreate.call_args.args[0]
            self.assertEqual(create.model.base_model_repo_id, 'acme/ChosenModel')
            self.assertEqual(create.serving_mode, self.api.ENDPOINT_SERVING_MODE_SHARED)
            self.assertTrue(create.shared_endpoint_notice_acknowledged)
            self.assertFalse(create.unauthenticated)
            self.assertTrue(client._stub.WebhookTokenCreate.call_args.args[0].scoped)
            token = Path(directory) / 'proxy.json'
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(token.read_text())['MODAL_PROXY_TOKEN_SECRET'], 'ws-Fixture')
            second = await self.bridge.dispatch(request, client)
            self.assertEqual(first, second)
            client._stub.EndpointCreate.assert_awaited_once()
            client._stub.WebhookTokenCreate.assert_awaited_once()
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'configuration changed'):
                await self.bridge.dispatch(dict(request, spec=dict(request['spec'], model='other/model')), client)
            client._stub.EndpointCreate.assert_awaited_once()

    async def test_managed_create_reconciles_uncertain_outcomes_without_replaying(self):
        import tempfile
        client = self.managed_client()
        create = client._stub.EndpointCreate.side_effect
        async def uncertain(request):
            await create(request)
            raise TimeoutError('upstream detail')
        client._stub.EndpointCreate.side_effect = uncertain
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            request = {'operation': 'endpoint_create', 'state_directory': directory,
                       'spec': {'name': 'example', 'model': 'moonshotai/Kimi-K3', 'configuration': {}}}
            with self.assertRaises(TimeoutError):
                await self.bridge.dispatch(request, client)
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'outcome is uncertain'):
                await self.bridge.dispatch(request, client)
            client._stub.EndpointCreate.assert_awaited_once()
            client._stub.EndpointGetByName.return_value = self.api.EndpointGetByNameResponse(endpoint_id=self.endpoint_id)
            result = await self.bridge.dispatch(request, client)
            self.assertEqual(result['resource']['provider_id'], self.endpoint_id)
            client._stub.EndpointCreate.assert_awaited_once()

    async def test_managed_token_uncertainty_stops_before_endpoint_creation(self):
        import tempfile
        client = self.managed_client()
        client._stub.WebhookTokenCreate.side_effect = TimeoutError('secret detail')
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            request = self.managed_request(directory)
            with self.assertRaises(TimeoutError):
                await self.bridge.dispatch(request, client)
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'Token creation outcome is uncertain'):
                await self.bridge.dispatch(request, client)
            client._stub.EndpointCreate.assert_not_awaited()
            client._stub.WebhookTokenCreate.assert_awaited_once()
            state = json.loads((Path(directory) / 'deployments.json').read_text())
            self.assertEqual(state['deployments'], {})

    async def test_definitive_rejection_allows_reconfiguration_after_permissions_are_fixed(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            request = self.managed_request(directory)
            client._stub.WebhookTokenCreate.side_effect = PermissionDeniedError()
            with self.assertRaises(PermissionDeniedError):
                await self.bridge.dispatch(request, client)
            client._stub.WebhookTokenCreate.side_effect = None
            original_create = client._stub.EndpointCreate.side_effect
            client._stub.EndpointCreate.side_effect = ConflictError()
            with self.assertRaises(ConflictError):
                await self.bridge.dispatch(request, client)
            client._stub.EndpointCreate.side_effect = original_create
            result = await self.bridge.dispatch(request, client)
            self.assertEqual(result['resource']['provider_id'], self.endpoint_id)
            self.assertEqual(client._stub.WebhookTokenCreate.await_count, 2)
            self.assertEqual(client._stub.EndpointCreate.await_count, 2)

    async def test_managed_status_deletion_and_ownership(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            await self.bridge.dispatch(self.managed_request(directory), client)
            request = {'operation': 'endpoint_status', 'endpoint_id': self.endpoint_id, 'state_directory': directory}
            result = await self.bridge.dispatch(request, client)
            self.assertEqual(result['state'], 'ready')
            self.assertIsNone(result['ready_replicas'])
            self.assertEqual(result['inference_model'], 'endpoint:' + self.endpoint_id)
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'not managed'):
                await self.bridge.dispatch(dict(request, endpoint_id='ep-' + 'B' * 22, operation='endpoint_delete'), client)
            client._stub.EndpointStop.assert_not_awaited()
            self.remote['model'] = 'other/model'
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'configuration changed'):
                await self.bridge.dispatch(dict(request, operation='endpoint_delete'), client)
            client._stub.EndpointStop.assert_not_awaited()
            self.remote['model'] = 'acme/ChosenModel'
            await self.bridge.dispatch(dict(request, operation='endpoint_delete'), client)
            client._stub.EndpointStop.assert_awaited_once()
            # A successful delete request alone is not proof of released capacity.
            self.assertEqual((await self.bridge.dispatch(request, client))['state'], 'ready')
            self.remote['lifecycle'] = self.api.ENDPOINT_LIFECYCLE_STATUS_STOPPED
            self.assertEqual((await self.bridge.dispatch(request, client))['state'], 'deleted')
            await self.bridge.dispatch(dict(request, operation='endpoint_delete'), client)
            client._stub.EndpointStop.assert_awaited_once()
            client._stub.EndpointGetInfo.side_effect = NotFoundError()
            self.assertEqual((await self.bridge.dispatch(request, client))['state'], 'deleted')
            client._stub.EndpointGetLifecycle.side_effect = NotFoundError()
            with self.assertRaises(NotFoundError):
                await self.bridge.dispatch(request, client)

    async def test_managed_inference_discovers_url_and_keeps_credentials_out_of_responses(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            await self.bridge.dispatch(self.managed_request(directory), client)
            request = {'operation': 'infer_endpoint', 'endpoint_id': self.endpoint_id, 'state_directory': directory,
                       'body': {'model': 'endpoint:' + self.endpoint_id, 'messages': [{'role': 'user', 'content': 'Hello'}]}}
            def inference(call, credential):
                self.assertEqual(call['endpoint'], 'https://fixture.us-west.modal.direct/v1')
                self.assertEqual(call['body']['model'], self.remote['name'])
                self.assertEqual(credential['MODAL_PROXY_TOKEN_SECRET'], 'ws-Fixture')
                # Model execution does not hold the state lock.
                with self.bridge.managed_state(dict(request, operation='endpoint_create'), 'ap-TestApp'):
                    pass
                return {'model': 'resolved', 'choices': []}
            with patch.object(self.bridge, 'inference', side_effect=inference):
                result = await self.bridge.dispatch(request, client)
            self.assertEqual(result['model'], 'resolved')
            self.assertNotIn('ws-Fixture', json.dumps(result))
            client._stub.EndpointCreate.assert_awaited_once()

    async def test_managed_state_refuses_public_credentials_and_directory_permissions(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            await self.bridge.dispatch(self.managed_request(directory), client)
            token = Path(directory) / 'proxy.json'
            token.chmod(0o644)
            request = {'operation': 'infer_endpoint', 'endpoint_id': self.endpoint_id, 'state_directory': directory, 'body': {}}
            with patch.object(self.bridge, 'inference') as inference:
                with self.assertRaisesRegex(self.bridge.ConfigurationError, 'must be private'):
                    await self.bridge.dispatch(request, client)
                inference.assert_not_called()
            token.chmod(0o600)
            Path(directory).chmod(0o755)
            try:
                with self.assertRaisesRegex(self.bridge.ConfigurationError, 'private directory'):
                    await self.bridge.dispatch(dict(request, operation='endpoint_delete'), client)
                client._stub.EndpointStop.assert_not_awaited()
            finally:
                Path(directory).chmod(0o700)

    async def test_managed_configuration_rejects_unsupported_options_before_mutation(self):
        import tempfile
        client = self.managed_client()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            for configuration in [[], {'unknown': True}, {'serving_mode': 'invalid'}, {'routing_region': 'invalid'}]:
                request = {'operation': 'endpoint_create', 'state_directory': directory,
                           'spec': {'name': 'example', 'model': 'moonshotai/Kimi-K3', 'configuration': configuration}}
                with self.assertRaises(self.bridge.ConfigurationError):
                    await self.bridge.dispatch(request, client)
            client._stub.EndpointCreate.assert_not_awaited()
            client._stub.WebhookTokenCreate.assert_not_awaited()

    async def test_inference_uses_proxy_token_without_workspace_client(self):
        request = {'operation': 'infer', 'endpoint': 'https://fixture.modal.run/v1/',
                   'body': {'model': 'model', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': False}}
        credential = {'MODAL_PROXY_TOKEN_ID': 'wk-fixture', 'MODAL_PROXY_TOKEN_SECRET': 'ws-fixture'}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"model":"resolved","choices":[]}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.bridge.urllib.request, 'build_opener', return_value=opener) as build:
            with patch.object(self.bridge.json, 'load', return_value=[request, credential]):
                self.assertEqual((await self.bridge.main())['model'], 'resolved')
        call = opener.open.call_args.args[0]
        self.assertEqual(call.full_url, 'https://fixture.modal.run/v1/chat/completions')
        self.assertEqual(call.get_header('Authorization'), 'Bearer wk-fixture.ws-fixture')
        self.assertEqual(json.loads(call.data), request['body'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 290)
        self.assertIsInstance(build.call_args.args[0], self.bridge.NoRedirect)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 307, '', {}, 'https://other.example'))
        self.modal.App.lookup.aio.assert_not_awaited()
        self.modal.Sandbox.create.aio.assert_not_awaited()

    def test_inference_rejects_invalid_targets_and_credentials_before_network(self):
        with patch.object(self.bridge.urllib.request, 'build_opener') as build:
            for url in ['http://fixture/v1', 'https://user:secret@fixture/v1', 'https://fixture/v1?secret=x',
                        'https://fixture/v1#fragment', 'https://fixture/\npath', '/v1']:
                with self.subTest(url=url), self.assertRaises(self.bridge.ConfigurationError):
                    self.bridge.inference({'endpoint': url}, {})
            with self.assertRaises(self.bridge.ConfigurationError):
                self.bridge.inference({'endpoint': 'https://fixture/v1'}, {})
            build.assert_not_called()

    def test_inference_bounds_responses_and_does_not_retry(self):
        request = {'endpoint': 'https://fixture.modal.run/v1', 'body': {}}
        credential = {'MODAL_PROXY_TOKEN_ID': 'wk-fixture', 'MODAL_PROXY_TOKEN_SECRET': 'ws-fixture'}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'x' * (2 * 1024 * 1024 + 1)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.bridge.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'exceeds'):
                self.bridge.inference(request, credential)
            opener.open.assert_called_once()
            opener.open.reset_mock()
            opener.open.side_effect = TimeoutError('private detail')
            with self.assertRaises(TimeoutError):
                self.bridge.inference(request, credential)
            opener.open.assert_called_once()

    async def test_configuration_saves_inference_endpoint(self):
        with patch.dict('os.environ', {}, clear=True):
            result = await self.bridge.dispatch({'operation': 'configure', 'options': {
                'inference_url': 'https://fixture.modal.run/v1/', 'inference_secret_file': '/private/proxy.json'}}, object())
        self.assertEqual(result['configuration']['MAGICMTN_MODAL_INFERENCE_URL'], 'https://fixture.modal.run/v1')
        self.assertEqual(result['configuration']['MAGICMTN_MODAL_INFERENCE_SECRET_FILE'], '/private/proxy.json')
        self.modal.App.lookup.aio.reset_mock()
        with self.assertRaises(self.bridge.ConfigurationError):
            await self.bridge.dispatch({'operation': 'configure', 'options': {'inference_url': 'http://bad'}}, object())
        self.modal.App.lookup.aio.assert_not_awaited()

    async def test_create_reattach_exec_and_terminate(self):
        with patch.dict('os.environ', {**self.mapping, 'MAGICMTN_MODAL_APP':'test-app'}):
            result = await self.bridge.dispatch({'operation':'create','image':self.image,'session':'session'})
        self.assertEqual(result['provider_id'], 'sb-test')
        self.assertNotIn('image_identity', result['metadata'])
        options = self.modal.Sandbox.create.aio.call_args.kwargs
        self.assertEqual(options['runtime'], 'vm')
        self.assertEqual(options['workdir'], '/workspace')
        self.assertEqual(options['image'], 'prepared-image')
        self.modal.Image.from_id.assert_called_once_with('im-verified', client=None)
        self.assertEqual(self.modal.Sandbox.create.aio.call_args.args[:3], ('/bin/su', '-s', '/bin/sh'))
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-1], 'sandbox')
        self.assertFalse(options['block_network'])
        self.assertEqual(options['timeout'], 3600)
        self.assertNotIn('idle_timeout', options)
        self.assertNotIn('secrets', options)
        self.assertNotIn('env', options)
        self.assertEqual(options['name'], 'magicmtn-session-session')
        self.assertEqual(options['memory'], (512,512))
        self.sandbox.exec.aio.return_value = process(7, [b'abcdefgh',b'ijkl'], [b'oops'])
        result = await self.bridge.dispatch(self.request)
        self.assertEqual(result['stdout'], 'abcd')
        self.assertEqual(result['stderr'], 'oops')
        self.assertEqual(result['exit_code'], 7)
        self.assertTrue(result['truncated'])
        self.sandbox.exec.aio.assert_called_with('/bin/su','-s','/bin/sh','-c',"exec echo 'literal; $value'",'sandbox',timeout=1,text=False,workdir='/workspace')
        self.sandbox.exec.aio.return_value = process(0, [b'\xff\xff'])
        result = await self.bridge.dispatch(self.request)
        self.assertLessEqual(len(result['stdout'].encode()), 4)
        self.assertTrue(result['truncated'])
        await self.bridge.dispatch({'operation':'terminate','sandbox':self.request['sandbox']})
        self.sandbox.terminate.aio.assert_awaited_once()

    async def test_configuration_creates_once_and_verification_never_creates(self):
        client = object()
        with patch.dict('os.environ', {}, clear=True):
            result = await self.bridge.dispatch({'operation': 'configure', 'options': {'app': 'my-app'}}, client)
        self.assertEqual(result, {'account': 'ap-TestApp', 'configuration': {'MAGICMTN_MODAL_APP': 'my-app', 'MAGICMTN_MODAL_APP_ID': 'ap-TestApp'}})
        self.modal.App.lookup.aio.assert_awaited_with('my-app', client=client, create_if_missing=True)
        with patch.dict('os.environ', result['configuration'], clear=True):
            self.assertEqual(await self.bridge.dispatch({'operation': 'configure', 'options': {}}, client), result)
            self.modal.App.lookup.aio.assert_awaited_with('my-app', client=client, create_if_missing=False)
            self.assertEqual(await self.bridge.dispatch({'operation': 'verify_account'}, client), 'ap-TestApp')
            self.modal.App.lookup.aio.assert_awaited_with('my-app', client=client, create_if_missing=False)
            self.modal.App.lookup.aio.return_value = SimpleNamespace(app_id='ap-Replacement')
            with self.assertRaisesRegex(self.bridge.ConfigurationError, 'differs'):
                await self.bridge.dispatch({'operation': 'configure', 'options': {}}, client)
            self.modal.App.lookup.aio.side_effect = RuntimeError('app missing')
            with self.assertRaisesRegex(RuntimeError, 'app missing'):
                await self.bridge.dispatch({'operation': 'verify_account'}, client)
            self.modal.App.lookup.aio.assert_awaited_with('my-app', client=client, create_if_missing=False)
        self.modal.Sandbox.create.aio.assert_not_awaited()

    async def test_configuration_validates_options_before_provider_calls(self):
        for options in ({'unknown': 'value'}, {'app': '../app'}, {'app': ''}, {'model': 'acme/model'}, {'inference_secret_file': 'relative.json'}):
            with self.assertRaises(self.bridge.ConfigurationError):
                await self.bridge.dispatch({'operation': 'configure', 'options': options}, object())
        self.modal.App.lookup.aio.assert_not_awaited()

    async def test_prepare_imports_the_exact_image_validates_and_cleans_up(self):
        image = SimpleNamespace(object_id='im-new', build=SimpleNamespace(aio=AsyncMock()))
        self.modal.Image.from_registry = Mock(return_value=image)
        client = object()
        with patch.dict('os.environ', {'MODAL_IMAGE_BUILDER_VERSION': '2023.12'}):
            result = await self.bridge.dispatch({'operation': 'prepare_image', 'image': self.image}, client)
            self.assertEqual(__import__('os').environ['MODAL_IMAGE_BUILDER_VERSION'], '2025.06')
        self.modal.Image.from_registry.assert_called_once_with(self.image)
        image.build.aio.assert_awaited_once_with(app=self.modal.App.lookup.aio.return_value)
        self.assertEqual(result['platform'], 'linux/amd64')
        self.assertEqual(result['artifact'], 'im-new')
        self.assertEqual(json.loads(result['configuration']['MAGICMTN_MODAL_PREPARED_IMAGES']), {self.image: 'im-new'})
        self.assertEqual(self.modal.Sandbox.create.aio.call_args.kwargs['timeout'], 120)
        self.sandbox.terminate.aio.assert_awaited_once()

    async def test_failed_image_validation_always_cleans_up(self):
        image = SimpleNamespace(object_id='im-new', build=SimpleNamespace(aio=AsyncMock()))
        self.modal.Image.from_registry = Mock(return_value=image)
        self.sandbox.exec.aio.return_value = process(1)
        with self.assertRaisesRegex(self.bridge.ConfigurationError, 'sandbox runtime'):
            await self.bridge.dispatch({'operation': 'prepare_image', 'image': self.image})
        self.sandbox.terminate.aio.assert_awaited_once()

    async def test_prepare_rejects_mutable_tags_before_any_provider_work(self):
        with self.assertRaises(self.bridge.ConfigurationError):
            await self.bridge.dispatch({'operation': 'prepare_image', 'image': 'example/image:latest'})
        self.modal.App.lookup.aio.assert_not_awaited()

    async def test_explicit_clients_verify_and_keep_access_after_rotation(self):
        first_client = object()
        second_client = object()
        constructor = AsyncMock(side_effect=[first_client, first_client, second_client, second_client, second_client])
        self.modal.Client = SimpleNamespace(from_credentials=SimpleNamespace(aio=constructor))

        async def invoke(request, token):
            payload = [request, {'MODAL_TOKEN_ID': token, 'MODAL_TOKEN_SECRET': 'fixture-secret'}]
            with patch.object(self.bridge.json, 'load', return_value=payload):
                return await self.bridge.main()

        verify = {'operation': 'verify_account'}
        create = {'operation': 'create', 'image': self.image, 'session': 'session'}
        with patch.dict('os.environ', {**self.mapping, 'MAGICMTN_MODAL_APP_ID': 'ap-TestApp'}):
            self.assertEqual(await invoke(verify, 'first-token'), 'ap-TestApp')
            sandbox = await invoke(create, 'first-token')
            self.assertEqual(await invoke(verify, 'second-token'), 'ap-TestApp')
            self.assertEqual(sandbox['provider_id'], self.request['sandbox']['provider_id'])
            await invoke(self.request, 'second-token')
            await invoke({'operation': 'terminate', 'sandbox': sandbox}, 'second-token')

        constructor.assert_any_await('first-token', 'fixture-secret')
        constructor.assert_awaited_with('second-token', 'fixture-secret')
        for invocation in self.modal.App.lookup.aio.await_args_list:
            self.assertFalse(invocation.kwargs['create_if_missing'])
        self.modal.Image.from_id.assert_called_once_with('im-verified', client=first_client)
        options = self.modal.Sandbox.create.aio.call_args.kwargs
        self.assertIs(options['client'], first_client)
        self.assertNotIn('env', options)
        self.assertNotIn('secrets', options)
        self.modal.Sandbox.from_id.aio.assert_awaited_with(sandbox['provider_id'], client=second_client)
        self.sandbox.terminate.aio.assert_awaited_once()

    async def test_changed_app_identity_fails_before_allocation(self):
        client = object()
        request = {'operation': 'create', 'image': self.image, 'session': 'session'}
        with patch.dict('os.environ', {**self.mapping, 'MAGICMTN_MODAL_APP_ID': 'ap-OtherApp'}):
            with self.assertRaises(self.bridge.ConfigurationError):
                await self.bridge.dispatch(request, client)
        self.modal.Sandbox.create.aio.assert_not_awaited()

    async def test_create_uses_requested_lifetime(self):
        with patch.dict('os.environ', self.mapping):
            await self.bridge.dispatch({'operation':'create','image':self.image,
                                        'session':'session','ttl_seconds':43200})
        self.assertEqual(self.modal.Sandbox.create.aio.call_args.kwargs['timeout'], 43200)
        with self.assertRaises(self.bridge.ConfigurationError):
            await self.bridge.dispatch({'operation':'create','image':self.image,
                                        'session':'session','ttl_seconds':86401})

    async def test_missing_malformed_or_unpinned_image_fails_before_allocation(self):
        for configuration, image in [('', self.image), ('{invalid', self.image),
                ('[]', self.image), ('{}', self.image),
                (json.dumps({self.image: 'not-an-image-id'}), self.image),
                (self.mapping['MAGICMTN_MODAL_PREPARED_IMAGES'], 'registry.example/sandbox:latest')]:
            with self.subTest(configuration=configuration, image=image):
                with patch.dict('os.environ', {'MAGICMTN_MODAL_PREPARED_IMAGES': configuration}):
                    with self.assertRaises(self.bridge.ConfigurationError):
                        await self.bridge.dispatch({'operation':'create','image':image,'session':'session'})
        self.modal.App.lookup.aio.assert_not_awaited()
        self.modal.Sandbox.create.aio.assert_not_awaited()

    async def test_failed_user_readiness_terminates_sandbox(self):
        self.sandbox.exec.aio.return_value = process(1)
        with patch.dict('os.environ', self.mapping):
            with self.assertRaisesRegex(RuntimeError, 'readiness'):
                await self.bridge.dispatch({'operation':'create','image':self.image,'session':'session'})
        self.sandbox.terminate.aio.assert_awaited_once()

    async def test_legacy_cleanup_does_not_require_image_configuration(self):
        self.sandbox.get_tags.aio.return_value = {
            'magicmtn':'modal','magicmtn_session':'session','magicmtn_image':self.image}
        with self.assertRaises(self.bridge.ConfigurationError):
            await self.bridge.dispatch(self.request)
        await self.bridge.dispatch({'operation':'terminate','sandbox':self.request['sandbox']})
        self.sandbox.terminate.aio.assert_awaited_once()
        self.sandbox.exec.aio.assert_not_awaited()

    async def test_prepared_image_identity_mismatch_rejects_execution(self):
        self.tags['magicmtn_prepared_image'] = 'im-other'
        with self.assertRaises(self.bridge.ConfigurationError):
            await self.bridge.dispatch(self.request)
        self.sandbox.exec.aio.assert_not_awaited()

    def test_shell_arguments_survive_user_switch_literally(self):
        command = ['echo', "quotes' and spaces", '$(id)', '; touch /tmp/unsafe', 'line\nfeed']
        wrapped = self.bridge.sandbox_command(command)
        self.assertEqual(wrapped[:4], ['/bin/su', '-s', '/bin/sh', '-c'])
        self.assertEqual(shlex.split(wrapped[4]), ['exec', *command])
        self.assertEqual(wrapped[5], 'sandbox')

    async def test_timeout_is_uncertain_and_not_replayed(self):
        self.sandbox.exec.aio.return_value = process(-1)
        result = await self.bridge.dispatch(self.request)
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['exit_code'])
        self.sandbox.exec.aio.assert_awaited_once()

    async def test_deadline_bounds_wait_and_output(self):
        async def stall():
            await asyncio.sleep(60)
        self.request['timeout_ms'] = 1
        self.sandbox.exec.aio.return_value.wait.aio.side_effect = stall
        result = await self.bridge.dispatch(self.request)
        self.assertTrue(result['timed_out'])
        self.sandbox.exec.aio.assert_awaited_once()

    async def test_rejects_changed_or_unmanaged_sandbox(self):
        self.sandbox.get_tags.aio.return_value = {'magicmtn':'modal','magicmtn_session':'session','magicmtn_image':'changed'}
        with self.assertRaises(RuntimeError):
            await self.bridge.dispatch(self.request)
        self.sandbox.get_tags.aio.return_value = {}
        with self.assertRaises(RuntimeError):
            await self.bridge.dispatch(self.request)
        self.sandbox.exec.aio.assert_not_awaited()
