"""Bundled bridge: workspace boundaries, checkpoints, evidence, OAuth pairing and MCP over real HTTP."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('bridge_under_test', ROOT / 'scripts' / 'bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
ws = b.ws


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com',
                            GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com'))


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.root = base / 'project'
        self.root.mkdir()
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'app.py').write_text('def main():\n    return "Needle"\n' + 'x = 1\n' * 500)
        (self.root / '.env').write_text('TOKEN=secret-value\n')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules' / 'dep.js').write_text('needle\n')
        (self.root / 'private.txt').write_text('needle in private\n')
        (self.root / '.c2cignore').write_text('private.txt\n')
        state = patch.dict(os.environ, {'ALPHA_C2C_STATE_DIR': str(base / 'state')})
        state.start()
        self.addCleanup(state.stop)
        self.w = ws.Workspace(self.root)


class WorkspaceTests(Fixture):
    def test_identity_matches_the_documented_hash(self):
        real = os.path.realpath(self.root)
        real = real.lower() if sys.platform in ('darwin', 'win32') else real
        self.assertEqual(self.w.id, hashlib.sha256(real.encode()).hexdigest()[:12])
        self.assertEqual(self.w.name, 'project')

    def test_paths_stay_inside_and_secrets_are_denied(self):
        self.assertEqual(self.w.resolve('workspace:/src/app.py')[1], 'src/app.py')
        for path, code in (('../outside', 'PATH_OUTSIDE_WORKSPACE'), ('.env', 'ACCESS_DENIED_SENSITIVE_FILE'),
                           ('private.txt', 'ACCESS_DENIED_SENSITIVE_FILE'), ('a\0b', 'INVALID_PATH')):
            with self.subTest(path=path), self.assertRaises(ws.ToolError) as caught:
                self.w.resolve(path)
            self.assertEqual(caught.exception.code, code)
        os.symlink('/', self.root / 'escape')
        with self.assertRaises(ws.ToolError):
            self.w.read_file('escape/etc/hosts')
        self.assertTrue(ws.matches(ws.compile_patterns(ws.SENSITIVE), 'config/.env.local'))
        self.assertFalse(ws.matches(ws.compile_patterns(ws.SENSITIVE), '.env.example'))

    def test_listing_and_search_hide_noise_and_sensitive_files(self):
        listed = [e['path'] for e in self.w.list_directory('.', depth=2)['entries']]
        self.assertIn('src/app.py', listed)
        self.assertFalse({'.env', 'node_modules/', 'private.txt'} & set(listed))
        found = self.w.search('needle')
        self.assertEqual([(m['path'], m['line']) for m in found['matches']], [('src/app.py', 2)])
        self.assertEqual(self.w.search('Needle', glob='*.js')['matches'], [])

    def test_read_file_pages_large_files(self):
        first = self.w.read_file('src/app.py')
        self.assertEqual((first['startLine'], first['endLine'], first['truncated']), (1, 400, True))
        rest = self.w.read_file('src/app.py', first['nextStartLine'])
        self.assertEqual((rest['endLine'], rest['truncated'], rest['nextStartLine']), (502, False, None))

    def test_git_status_and_diff_never_reveal_sensitive_changes(self):
        git(self.root, 'init', '-q')
        git(self.root, 'add', '-f', 'src/app.py', '.env')
        git(self.root, 'commit', '-qm', 'base')
        (self.root / 'src' / 'app.py').write_text('changed\n')
        (self.root / '.env').write_text('TOKEN=rotated\n')
        status = self.w.git_status()
        self.assertEqual(status['unstaged'], [{'path': 'src/app.py', 'change': 'modified'}])
        self.assertEqual(status['hidden']['changes'], 2)  # .env edit and the .c2cignore'd untracked file.
        self.assertNotIn('private.txt', status['untracked'])
        diff = self.w.git_diff()
        self.assertIn('+changed', diff['diff'])
        self.assertNotIn('rotated', diff['diff'])
        page = self.w.git_diff(max_bytes=1024)
        self.assertTrue(page['hasMore'])
        self.assertTrue(self.w.info()['git']['dirty'])


class SessionAndEvidenceTests(Fixture):
    def args(self, **values):
        base = dict(url=None, title=None, task=None, state=None, project_url=None, connector_name=None,
                    protocol_state=None, waiting_for=None, goal=None, completed_subtasks=None, known_issues=None,
                    next_step=None, iteration=None, mode=None, clear_checkpoint=False)
        return argparse.Namespace(**dict(base, **values))

    def test_checkpoint_merges_and_keeps_urls_aligned(self):
        self.assertIsNone(b.session_get(self.w))
        b.session_set(self.w, self.args(url='https://chatgpt.com/c/abc?x=1', task='t1', protocol_state='plan_received',
                                        iteration=2))
        saved = b.session_set(self.w, self.args(protocol_state='EXECUTING', waiting_for='none'))
        cp = saved['checkpoint']
        self.assertEqual((cp['taskId'], cp['iteration'], cp['protocolState'], cp['waitingFor']), ('t1', 2, 'EXECUTING', 'none'))
        self.assertEqual((saved['url'], cp['chatUrl']), ('https://chatgpt.com/c/abc',) * 2)
        for bad in (dict(url='https://evil.example/c/1'), dict(protocol_state='LATER'), dict(mode='project')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                b.session_set(self.w, self.args(**bad))
        b.session_set(self.w, self.args(clear_checkpoint=True))
        self.assertNotIn('checkpoint', b.session_get(self.w))

    def test_recorded_output_is_sanitized_and_private_keys_are_withheld(self):
        log = Path(self.root.parent / 'out.txt')
        log.write_text('ok at /Users/example/x with sk-%s and password=hunter2\n' % ('a' * 24))
        rec = argparse.Namespace(task='t1', iteration=1, changed_files='a.py,b.py', tests='unit', exit_status='ok',
                                 notes=None, command='pytest', output=None, output_file=str(log), exit_code=0)
        entry = b.record(self.w, rec)
        text = b.output_tool(self.w, 'read', entry['outputId'])['text']
        self.assertNotIn('hunter2', text)
        self.assertNotIn('aaaaaaaaaaaaaaaa', text)
        self.assertIn('/Users/[user]', text)
        log.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n')
        withheld = b.record(self.w, rec)
        with self.assertRaises(ws.ToolError) as caught:
            b.output_tool(self.w, 'read', withheld['outputId'])
        self.assertEqual(caught.exception.code, 'OUTPUT_RESTRICTED')
        self.assertEqual(b.call_tool(self.w, 'test_status', {})['outputId'], withheld['outputId'])
        self.assertEqual(len(b.call_tool(self.w, 'execution_summary', {})['records']), 2)

    def test_pairing_codes_are_single_use_and_limited(self):
        pairing = b.Pairing()
        code, _ = pairing.create()
        self.assertEqual(pairing.verify('wrong', 'ip'), 'invalid')
        self.assertEqual(pairing.verify(code.lower(), 'ip'), 'ok')
        self.assertEqual(pairing.verify(code, 'ip'), 'no_session')
        pairing.create()
        verdicts = [pairing.verify('nope', 'other') for _ in range(6)]
        self.assertEqual(verdicts[-1], 'too_many_attempts')


class FakeTunnel:
    def __init__(self, port, workspace_id):
        self.url = None

    def start(self):
        self.url = 'https://fixture.trycloudflare.com'
        return self.url

    def running(self):
        return bool(self.url)

    def stop(self):
        self.url = None


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = b.Bridge(self.w, 0, tunnel_factory=FakeTunnel)
        self.server.write_runtime()
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.server.port

    def call(self, method, path, data=None, headers=None, form=False):
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault('Content-Type', 'application/x-www-form-urlencoded' if form else 'application/json')
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with OPENER.open(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers), error.read().decode()

    def connect(self):
        status, _, body = self.call('POST', '/oauth/register', {'redirect_uris': ['https://chatgpt.com/connector_platform_oauth_redirect'],
                                                                'client_name': 'ChatGPT'})
        self.assertEqual(status, 201)
        client = json.loads(body)['client_id']
        verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query = urllib.parse.urlencode({'client_id': client, 'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect',
                                        'response_type': 'code', 'code_challenge': challenge,
                                        'code_challenge_method': 'S256', 'state': 'st', 'scope': ' '.join(b.SCOPES)})
        status, headers, page = self.call('GET', '/oauth/authorize?' + query)
        self.assertEqual(status, 200)
        self.assertIn('frame-ancestors https://chatgpt.com', headers['Content-Security-Policy'])
        request_id = page.split('name="request_id" value="')[1].split('"')[0]
        code, _ = self.server.pairing.create()
        status, _, _ = self.call('POST', '/oauth/authorize', {'request_id': request_id, 'pairing_code': 'AAAA-AAAA'}, form=True)
        self.assertEqual(status, 401)
        status, headers, _ = self.call('POST', '/oauth/authorize', {'request_id': request_id, 'pairing_code': code}, form=True)
        self.assertEqual(status, 302)
        location = urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
        self.assertEqual(location['state'], ['st'])
        token_request = {'grant_type': 'authorization_code', 'code': location['code'][0], 'client_id': client,
                         'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect'}
        status, _, _ = self.call('POST', '/oauth/token', dict(token_request, code_verifier='wrong'), form=True)
        self.assertEqual(status, 400)  # A failed PKCE attempt also burns the code.
        return client, verifier, challenge

    def full_token(self):
        client, verifier, challenge = self.connect()
        query = urllib.parse.urlencode({'client_id': client, 'response_type': 'code', 'code_challenge': challenge,
                                        'code_challenge_method': 'S256'})
        page = self.call('GET', '/oauth/authorize?' + query)[2]
        request_id = page.split('name="request_id" value="')[1].split('"')[0]
        code, _ = self.server.pairing.create()
        headers = self.call('POST', '/oauth/authorize', {'request_id': request_id, 'pairing_code': code}, form=True)[1]
        auth_code = urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)['code'][0]
        status, _, body = self.call('POST', '/oauth/token', {'grant_type': 'authorization_code', 'code': auth_code,
                                                             'client_id': client, 'code_verifier': verifier}, form=True)
        self.assertEqual(status, 200)
        return client, json.loads(body)

    def mcp(self, token, message):
        return self.call('POST', '/mcp', message, {'Authorization': 'Bearer ' + token,
                                                   'Accept': 'application/json, text/event-stream'})

    def test_discovery_points_at_this_server(self):
        status, _, body = self.call('GET', '/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(json.loads(body)['resource'], self.base + '/mcp')
        meta = json.loads(self.call('GET', '/.well-known/oauth-authorization-server')[2])
        self.assertEqual((meta['token_endpoint'], meta['code_challenge_methods_supported']), (self.base + '/oauth/token', ['S256']))

    def test_unauthenticated_mcp_points_to_resource_metadata(self):
        status, headers, _ = self.call('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="%s/.well-known/oauth-protected-resource/mcp"' % self.base, headers['WWW-Authenticate'])
        self.assertEqual(self.call('GET', '/mcp')[0], 405)

    def test_paired_client_can_initialize_list_and_call_tools(self):
        client, tokens = self.full_token()
        token = tokens['access_token']
        status, _, body = self.mcp(token, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                           'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                                      'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertEqual(json.loads(body)['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(self.mcp(token, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})[0], 202)
        tools = json.loads(self.mcp(token, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})[2])['result']['tools']
        names = [t['name'] for t in tools]
        self.assertEqual(len(names), 9)
        self.assertTrue(all(n.startswith('c2c_%s_' % self.w.id) and t['annotations']['readOnlyHint'] for n, t in zip(names, tools)))
        reply = json.loads(self.mcp(token, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                            'params': {'name': 'c2c_%s_workspace_info' % self.w.id, 'arguments': {}}})[2])
        self.assertEqual(reply['result']['structuredContent']['workspaceId'], self.w.id)
        denied = json.loads(self.mcp(token, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                                             'params': {'name': 'c2c_%s_read_file' % self.w.id, 'arguments': {'path': '.env'}}})[2])
        self.assertTrue(denied['result']['isError'])
        self.assertIn('ACCESS_DENIED_SENSITIVE_FILE', denied['result']['content'][0]['text'])
        # Refresh rotates: the old refresh token is spent.
        refreshed = self.call('POST', '/oauth/token', {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                                                       'client_id': client}, form=True)
        self.assertEqual(refreshed[0], 200)
        again = self.call('POST', '/oauth/token', {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                                                   'client_id': client}, form=True)
        self.assertEqual(again[0], 400)
        self.assertGreaterEqual(self.server.store.count(), 2)

    def test_admin_api_needs_the_local_token_and_refuses_tunnelled_requests(self):
        token = {'Authorization': 'Bearer ' + self.server.admin_token}
        self.assertEqual(self.call('GET', '/admin/info')[0], 404)
        self.assertEqual(self.call('GET', '/admin/info', headers=dict(token, **{'Cf-Connecting-Ip': '1.2.3.4'}))[0], 404)
        info = json.loads(self.call('GET', '/admin/info', headers=token)[2])
        self.assertEqual((info['workspaceId'], info['tokenCount']), (self.w.id, 0))
        started = json.loads(self.call('POST', '/admin/tunnel/start', headers=token)[2])
        self.assertEqual(started['publicUrl'], 'https://fixture.trycloudflare.com')
        meta = json.loads(self.call('GET', '/.well-known/oauth-authorization-server')[2])
        self.assertEqual(meta['issuer'], 'https://fixture.trycloudflare.com')
        runtime = b.read_json(b.runtime_path(self.w))
        self.assertEqual(b.observe(self.w)[0], 'healthy')
        self.assertEqual(b.status_data(self.w)['publicUrl'], 'https://fixture.trycloudflare.com')
        self.assertEqual(runtime['publicUrl'], 'https://fixture.trycloudflare.com')


class TunnelTests(unittest.TestCase):
    def test_quick_tunnel_reads_the_address_and_waits_for_health(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'cloudflared'
            fake.write_text('#!/bin/sh\necho "INF Requesting new quick Tunnel on api.trycloudflare.com..."\n'
                            'echo "INF |  https://quiet-river-1.trycloudflare.com  |"\nsleep 30\n')
            fake.chmod(0o755)
            seen = []
            tunnel = b.QuickTunnel(1, 'ws', binary=str(fake), health=lambda url, wid: seen.append(url) or len(seen) > 1)
            try:
                self.assertEqual(tunnel.start(), 'https://quiet-river-1.trycloudflare.com')
                self.assertTrue(tunnel.running())
            finally:
                tunnel.stop()
            self.assertFalse(tunnel.running())

    def test_missing_cloudflared_is_reported(self):
        with patch.object(b, 'find_cloudflared', return_value=None), self.assertRaisesRegex(RuntimeError, 'cloudflared'):
            b.QuickTunnel(1, 'ws').start()


class CliTests(Fixture):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / 'bridge.py'), *args, '-w', str(self.root)],
                              capture_output=True, text=True, timeout=60)

    def test_status_is_json_and_exit_zero_when_stopped(self):
        result = self.run_cli('status', '--json')
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual((data['running'], data['workspaceId']), (False, self.w.id))
        self.assertEqual(json.loads(self.run_cli('session', 'get', '--json').stdout), {'ok': True, 'session': None})

    def test_daemon_starts_pairs_and_stops(self):
        try:
            self.assertEqual(self.run_cli('start', '--json').returncode, 0)
            status = json.loads(self.run_cli('status', '--json').stdout)
            self.assertEqual((status['running'], status['tokenCount']), (True, 0))
            paired = json.loads(self.run_cli('pair', '--json').stdout)
            self.assertRegex(paired['pairingCode'], r'^[A-Z2-9]{4}-[A-Z2-9]{4}$')
        finally:
            self.assertEqual(self.run_cli('stop').returncode, 0)
        self.assertFalse(json.loads(self.run_cli('status', '--json').stdout)['running'])

    def test_status_adapter_reads_the_bundled_bridge(self):
        spec_ = importlib.util.spec_from_file_location('c2c_adapter', ROOT / 'scripts' / 'c2c.py')
        c2c = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(c2c)
        summary = c2c.query_status(self.root, None, b.EXECUTOR)
        self.assertEqual(summary['state'], 'stopped')
        self.assertEqual(summary['expectedConnector'], b.connector_name(self.w))


if __name__ == '__main__':
    unittest.main()
