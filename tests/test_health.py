"""The health endpoint must work without settings or AWS clients."""

import importlib
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class HealthTests(unittest.TestCase):
    def test_health_ok_without_aws(self):
        no_aws = AssertionError("The health endpoint must not create AWS clients.")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("boto3.client", side_effect=no_aws) as client_factory,
            patch("boto3.resource", side_effect=no_aws) as resource_factory,
            patch("boto3.session.Session.client", side_effect=no_aws)
            as session_client_factory,
            patch("boto3.session.Session.resource", side_effect=no_aws)
            as session_resource_factory,
        ):
            api_main = importlib.import_module("api.main")
            api_main = importlib.reload(api_main)
            with TestClient(api_main.app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})
            for factory in (
                client_factory,
                resource_factory,
                session_client_factory,
                session_resource_factory,
            ):
                factory.assert_not_called()
