"""Deployment edits preserve unrelated sites and existing production secrets."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from cryptography.fernet import Fernet

spec = importlib.util.spec_from_file_location('workspace_config', Path(__file__).resolve().parents[1]/'deploy/workspace_config.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class DeploymentTests(unittest.TestCase):
    def test_caddy_changes_only_selected_site_and_is_idempotent(self):
        original = 'other.example {\n reverse_proxy 127.0.0.1:8080\n}\nzeus.example {\n encode gzip\n reverse_proxy 127.0.0.1:8080 # Zeus\n}\n'
        result = deployment.caddy_target(original, 'zeus.example')
        self.assertIn('other.example {\n reverse_proxy 127.0.0.1:8080\n}', result)
        self.assertIn('reverse_proxy 127.0.0.1:8081 # Zeus', result)
        self.assertEqual(deployment.caddy_target(result, 'zeus.example'), result)

    def test_unknown_or_ambiguous_upstreams_are_not_rewritten(self):
        for text in ('another.example {\n reverse_proxy 127.0.0.1:8080\n}\n',
                     'zeus.example {\n reverse_proxy 127.0.0.1:8080\n reverse_proxy 127.0.0.1:8080\n}\n'):
            with self.assertRaises(ValueError): deployment.caddy_target(text, 'zeus.example')

    def test_public_origin_must_be_an_https_origin(self):
        self.assertEqual(deployment.public_origin('https://zeus.example/'), 'https://zeus.example')
        for value in ('http://zeus.example', 'https://user@zeus.example', 'https://zeus.example/app'):
            with self.assertRaises(ValueError): deployment.public_origin(value)

    def test_existing_key_and_environment_are_never_regenerated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = Fernet.generate_key().decode()
            config = ('ZEUS_WORKSPACE_PRODUCTION=1\nZEUS_WORKSPACE_ORIGIN=https://zeus.example\n'
                      'ZEUS_WORKSPACE_DATABASE_URL=postgresql+psycopg://test@127.0.0.1/test\n'
                      f'ZEUS_WORKSPACE_KEY={key}\n')
            path = root/'.env.workspace'; path.write_text(config)
            with patch('sqlalchemy.create_engine', return_value=MagicMock()) as engine, patch.object(deployment, 'psql') as admin:
                deployment.configure(root, 'https://zeus.example')
                self.assertEqual(path.read_text(), config)
                admin.assert_not_called(); engine.return_value.dispose.assert_called_once()
            with self.assertRaises(ValueError): deployment.configure(root, 'https://different.example')
            self.assertEqual(path.read_text(), config)
