"""Docker bootstrap and HTTP security, using only temporary synthetic data."""
import os
from pathlib import Path
import re
import tempfile
import unittest

from server.container_entry import application, settings, INITIAL_PASSWORD

ORIGIN = 'http://localhost:41880'


def login(client, password, origin=ORIGIN):
    page = client.get('/auth/login', base_url=origin)
    nonce = re.search('name="nonce" value="([^"]+)"', page.text).group(1)
    return client.post('/auth/login', base_url=origin, data={'nonce': nonce, 'password': password}, headers={'Origin': origin})


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'VIEWER_DATA_DIR': str(self.root / 'data'), 'VIEWER_PUBLIC_ORIGIN': ORIGIN, 'VIEWER_LOCAL_HTTP': '1'}

    def boot(self, env=None):
        return application(env or self.env, log=lambda *_: None)

    def test_generated_initial_password_is_private_persistent_and_removed_after_change(self):
        app, config = self.boot()
        initial = config.data_dir / INITIAL_PASSWORD; password = initial.read_text().strip()
        self.assertGreaterEqual(len(password), 20)
        if os.name != 'nt':
            self.assertEqual(initial.stat().st_mode & 0o777, 0o600)
        secret = config.session_file.read_bytes()
        again, _ = self.boot()
        self.assertEqual(initial.read_text().strip(), password, 'restart never regenerates or overwrites')
        self.assertEqual(config.session_file.read_bytes(), secret); self.assertEqual(app.secret_key, again.secret_key)
        client = again.test_client()
        self.assertEqual(login(client, 'wrong password!').status_code, 401)
        self.assertEqual(login(client, password).status_code, 302)
        csrf = re.search('name="cloud-csrf" content="([^"]+)"', client.get('/', base_url=ORIGIN).text).group(1)
        response = client.post('/api/account/password', base_url=ORIGIN, json={'currentPassword': password, 'newPassword': 'a much better password'},
                               headers={'X-CSRF-Token': csrf, 'Origin': ORIGIN})
        self.assertEqual(response.status_code, 200); self.assertFalse(initial.exists())
        self.assertEqual(client.get('/api/keys', base_url=ORIGIN).status_code, 200, 'the changing browser stays signed in')
        self.boot(); self.assertFalse(initial.exists())

    def test_external_password_file_is_used_once_and_never_modified(self):
        secret = self.root / 'admin.password'; secret.write_text('provided owner password\n')
        before = secret.read_bytes()
        app, config = self.boot({**self.env, 'VIEWER_ADMIN_PASSWORD_FILE': str(secret)})
        self.assertFalse((config.data_dir / INITIAL_PASSWORD).exists())
        self.assertEqual(login(app.test_client(), 'provided owner password').status_code, 302)
        self.assertEqual(secret.read_bytes(), before)

    def test_health_login_headers_and_unauthenticated_access(self):
        app, _ = self.boot(); client = app.test_client()
        health = client.get('/healthz', base_url=ORIGIN)
        self.assertEqual(health.status_code, 200); self.assertTrue(health.json['ok'])
        self.assertNotIn('Set-Cookie', health.headers); self.assertNotIn('sessions', health.json)
        page = client.get('/auth/login', base_url=ORIGIN)
        self.assertEqual(page.headers['Referrer-Policy'], 'same-origin')
        self.assertFalse(app.config['SESSION_COOKIE_SECURE'])
        for path in ('/api/sessions', '/api/keys', '/static/js/app.js', '/api/sources'):
            self.assertEqual(client.get(path, base_url=ORIGIN).status_code, 401, path)
        self.assertEqual(client.get('/', base_url=ORIGIN).status_code, 302)
        self.assertEqual(client.get('/healthz', base_url='http://evil.test').status_code, 403)

    def test_https_origin_uses_secure_cookies(self):
        app, _ = self.boot({**self.env, 'VIEWER_PUBLIC_ORIGIN': 'https://history.example.com', 'VIEWER_LOCAL_HTTP': '0'})
        self.assertTrue(app.config['SESSION_COOKIE_SECURE'])
        self.assertEqual(app.test_client().get('/healthz', base_url='https://history.example.com').status_code, 200)

    def test_new_default_database_never_reuses_the_retired_file(self):
        _, config = self.boot()
        self.assertEqual(config.database.name, 'cloud-v6.db')

    def test_refuse_nonloopback_http_and_database_escape(self):
        for env in [
            {**self.env, 'VIEWER_PUBLIC_ORIGIN': 'http://history.example.com'},
            {**self.env, 'VIEWER_LOCAL_HTTP': '0'},
            {**self.env, 'VIEWER_PUBLIC_ORIGIN': 'https://user:password@history.example.com'},
            {**self.env, 'VIEWER_PUBLIC_ORIGIN': 'https://history.example.com/path'},
            {**self.env, 'VIEWER_DATABASE': str(self.root / 'outside.db')},
            {**self.env, 'VIEWER_DATA_DIR': str(Path(self.root.anchor))},
            {**self.env, 'VIEWER_PORT': '0'},
            {**self.env, 'VIEWER_MAX_REQUEST_MIB': '0'},
        ]:
            with self.subTest(config=set(env)):
                with self.assertRaises(ValueError):
                    settings(env)

    def test_gzip_response_hook_preserves_json_and_respects_quality(self):
        import gzip
        app, _ = self.boot()
        from server.http_compression import compress_response
        with app.test_request_context(headers={'Accept-Encoding': 'gzip'}):
            response = compress_response(app.json.response({'text': 'synthetic ' * 1000}))
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertIn(b'synthetic', gzip.decompress(response.get_data()))
        with app.test_request_context(headers={'Accept-Encoding': 'gzip;q=0'}):
            response = compress_response(app.json.response({'text': 'synthetic ' * 1000}))
            self.assertNotIn('Content-Encoding', response.headers)

    def test_cross_platform_subagent_path_classification(self):
        from server.contracts.protocol import is_subagent
        self.assertTrue(is_subagent('claude', r'C:\Users\test\.claude\projects\project\agent-child.jsonl'))
        self.assertTrue(is_subagent('claude', r'C:\Users\test\.claude\projects\project\sid\subagents\child.jsonl'))
        self.assertFalse(is_subagent('claude', r'C:\Users\test\.claude\projects\project\ordinary.jsonl'))

    def test_docker_copy_and_ignore_rules_keep_secrets_out_of_images(self):
        directory = Path(__file__).resolve().parents[1] / 'deploy'
        dockerfile = (directory / 'Dockerfile').read_text()
        for forbidden in ('COPY server/ ./', 'COPY . ', 'COPY local', 'COPY sources/'):
            self.assertNotIn(forbidden, dockerfile)
        self.assertIn('COPY server/*.py', dockerfile)
        ignore = (directory / 'Dockerfile.dockerignore').read_text()
        for rule in ('local/**', '**/secrets/**', '**/state/**', '**/*.key', '**/*.db', '**/*.jsonl', '**/.git/**', '**/*password*'):
            self.assertIn(rule, ignore)

    def test_explicit_session_secret_file_is_never_generated(self):
        key = self.root / 'external.key'
        with self.assertRaises(FileNotFoundError):
            self.boot({**self.env, 'VIEWER_SESSION_KEY_FILE': str(key)})
        self.assertFalse(key.exists())


if __name__ == '__main__':
    unittest.main()
