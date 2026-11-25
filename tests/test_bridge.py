import asyncio
import json
import shlex
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


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
        with patch.dict(sys.modules, {'modal': self.modal, 'modal.exception': SimpleNamespace(ExecTimeoutError=ExecTimeoutError)}):
            spec.loader.exec_module(self.bridge)
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
        for options in ({'unknown': 'value'}, {'app': '../app'}, {'app': ''}):
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
