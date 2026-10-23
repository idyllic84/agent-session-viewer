"""Docker bootstrap security/readiness, using only temporary synthetic data."""
from pathlib import Path
import tempfile
import unittest

from server.container_entry import application,settings


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.env={'VIEWER_DATA_DIR':str(self.root/'data'),'VIEWER_PUBLIC_ORIGIN':'http://localhost:41880','VIEWER_LOCAL_HTTP':'1'}

    def test_local_bootstrap_keeps_owner_and_cookie_secrets_across_restarts(self):
        app,config=application(self.env)
        owner=config.owner_file.read_bytes();secret=config.session_file.read_bytes()
        self.assertNotEqual(owner,secret)
        self.assertGreaterEqual(len(owner.strip()),32)
        self.assertFalse(app.config['SESSION_COOKIE_SECURE'])
        client=app.test_client();response=client.get('/healthz',base_url='http://localhost:41880')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['ok'])
        self.assertNotIn('Set-Cookie',response.headers)
        self.assertNotIn('sessions',response.json)
        # no-referrer makes browser navigation-form POST Origin opaque/null;
        # same-origin retains login CSRF checks without third-party referrers.
        login=client.get('/auth/login',base_url='http://localhost:41880')
        self.assertEqual(login.headers['Referrer-Policy'],'same-origin')
        self.assertEqual(client.get('/api/sessions',base_url='http://localhost:41880').status_code,401)
        self.assertEqual(client.get('/healthz',base_url='http://evil.test').status_code,403)
        again,_=application(self.env)
        self.assertEqual(config.owner_file.read_bytes(),owner)
        self.assertEqual(config.session_file.read_bytes(),secret)
        self.assertEqual(app.secret_key,again.secret_key)
        self.assertTrue(config.database.exists())

    def test_gzip_response_hook_preserves_json_and_respects_quality(self):
        import gzip
        app,_=application(self.env)
        # Test hook directly in a request context; auth is covered separately.
        from server.http_compression import compress_response
        with app.test_request_context(headers={'Accept-Encoding':'gzip'}):
            response=compress_response(app.json.response({'text':'synthetic '*1000}))
            self.assertEqual(response.headers['Content-Encoding'],'gzip')
            self.assertIn('Accept-Encoding',response.headers['Vary'])
            self.assertIn(b'synthetic',gzip.decompress(response.get_data()))
        with app.test_request_context(headers={'Accept-Encoding':'gzip;q=0'}):
            response=compress_response(app.json.response({'text':'synthetic '*1000}))
            self.assertNotIn('Content-Encoding',response.headers)

    def test_cross_platform_subagent_path_classification(self):
        from server.contracts.protocol import is_subagent
        self.assertTrue(is_subagent('claude',r'C:\Users\test\.claude\projects\project\agent-child.jsonl'))
        self.assertTrue(is_subagent('claude',r'C:\Users\test\.claude\projects\project\sid\subagents\child.jsonl'))
        self.assertFalse(is_subagent('claude',r'C:\Users\test\.claude\projects\project\ordinary.jsonl'))

    def test_https_origin_uses_secure_cookies(self):
        app,_=application({**self.env,'VIEWER_PUBLIC_ORIGIN':'https://history.example.com','VIEWER_LOCAL_HTTP':'0'})
        self.assertTrue(app.config['SESSION_COOKIE_SECURE'])
        self.assertEqual(app.test_client().get('/healthz',base_url='https://history.example.com').status_code,200)

    def test_refuse_nonloopback_http_and_database_escape(self):
        for env in [
            {**self.env,'VIEWER_PUBLIC_ORIGIN':'http://history.example.com'},
            {**self.env,'VIEWER_LOCAL_HTTP':'0'},
            {**self.env,'VIEWER_PUBLIC_ORIGIN':'https://user:password@history.example.com'},
            {**self.env,'VIEWER_PUBLIC_ORIGIN':'https://history.example.com/path'},
            {**self.env,'VIEWER_DATABASE':str(self.root/'outside.db')},
            {**self.env,'VIEWER_DATA_DIR':str(Path(self.root.anchor))},
            {**self.env,'VIEWER_PORT':'0'},
            {**self.env,'VIEWER_MAX_REQUEST_MIB':'0'},
        ]:
            with self.subTest(config=set(env)):
                with self.assertRaises(ValueError):settings(env)

    def test_docker_copy_and_ignore_rules_keep_secrets_out_of_images(self):
        directory=Path(__file__).resolve().parents[1]/'deploy'
        dockerfile=(directory/'Dockerfile').read_text()
        self.assertNotIn('COPY server/ ./',dockerfile)
        self.assertNotIn('COPY sources/ ./',dockerfile)
        self.assertNotIn('COPY . ',dockerfile)
        self.assertIn('COPY server/*.py',dockerfile)
        ignore=(directory/'Dockerfile.dockerignore').read_text()
        for rule in ('**/secrets/**','**/state/**','**/*.key','**/*.db','**/*.jsonl','**/.git/**'):
            self.assertIn(rule,ignore)

    def test_explicit_secret_file_is_never_generated_or_overwritten(self):
        key=self.root/'external.key'
        with self.assertRaises(FileNotFoundError):application({**self.env,'VIEWER_OWNER_KEY_FILE':str(key)})
        self.assertFalse(key.exists())
        key.write_text('external-owner-'*5)
        before=key.read_bytes();application({**self.env,'VIEWER_OWNER_KEY_FILE':str(key)})
        self.assertEqual(key.read_bytes(),before)


if __name__=='__main__':unittest.main()
