"""Verify the installed SDK imports without contacting Modal."""
import importlib.metadata
import unittest

from modal_proto import api_pb2
import modal
from modal.exception import ExecTimeoutError


class ModalImportTest(unittest.TestCase):
    def test_sdk_matches_the_bridge_contract(self):
        self.assertEqual(importlib.metadata.version("modal"), "1.6.1")
        self.assertTrue(callable(modal.Client.from_credentials.aio))
        self.assertTrue(callable(modal.App.lookup.aio))
        self.assertTrue(callable(modal.Image.from_id))
        self.assertTrue(callable(modal.Image.from_registry))
        self.assertTrue(callable(modal.Image.build.aio))
        self.assertTrue(callable(modal.Sandbox.create.aio))
        self.assertTrue(callable(modal.Sandbox.from_id.aio))
        self.assertTrue(callable(modal.Environment.from_name))
        self.assertTrue(callable(modal.Environment.from_context))
        self.assertIn("serving_mode", api_pb2.EndpointCreateRequest.DESCRIPTOR.fields_by_name)
        self.assertIn("shared_endpoint_notice_acknowledged", api_pb2.EndpointCreateRequest.DESCRIPTOR.fields_by_name)
        self.assertIn("scoped", api_pb2.WebhookTokenCreateRequest.DESCRIPTOR.fields_by_name)
        self.assertTrue(issubclass(ExecTimeoutError, Exception))


if __name__ == "__main__":
    unittest.main()
