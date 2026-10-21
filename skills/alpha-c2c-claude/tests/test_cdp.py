"""Chrome DevTools backend: websocket codec, loopback-only policy and verb mapping."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


c = load('cdp_under_test', 'cdp.py')
w = load('cdp_waiter', 'wait_reply.py')


def server_frame(payload, opcode=1, fin=True):
    size = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        head += bytes([size])
    elif size < 1 << 16:
        head += bytes([126]) + struct.pack('>H', size)
    else:
        head += bytes([127]) + struct.pack('>Q', size)
    return head + payload


def unmask(frame):
    size, offset = frame[1] & 0x7F, 2
    if size == 126:
        size, offset = struct.unpack('>H', frame[2:4])[0], 4
    elif size == 127:
        size, offset = struct.unpack('>Q', frame[2:10])[0], 10
    mask = frame[offset:offset + 4]
    return frame[0] & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(frame[offset + 4:offset + 4 + size]))


class FakeSocket:
    def __init__(self, incoming=b'', accept=True):
        self.incoming, self.sent, self.accept, self.closed = incoming, [], accept, False

    def sendall(self, data):
        self.sent.append(data)
        if data.startswith(b'GET '):
            key = [l.split(b': ')[1] for l in data.split(b'\r\n') if l.startswith(b'Sec-WebSocket-Key')][0].decode()
            digest = base64.b64encode(hashlib.sha1((key + c.GUID).encode()).digest()).decode()
            accept = digest if self.accept else 'wrong'
            self.incoming = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: '
                             + accept.encode() + b'\r\n\r\n') + self.incoming

    def recv(self, size):
        data, self.incoming = self.incoming[:size], self.incoming[size:]
        return data

    def close(self):
        self.closed = True


def socket_with(incoming=b'', accept=True):
    fake = FakeSocket(incoming, accept)
    return fake, c.WebSocket('ws://127.0.0.1:9222/devtools/page/X', connect=lambda address, timeout: fake)


class CodecTests(unittest.TestCase):
    def test_client_frames_are_masked_for_all_length_encodings(self):
        for size in (5, 300, 70000):
            payload = os.urandom(size)
            opcode, data = unmask(c.encode_frame(payload))
            self.assertEqual((opcode, data), (1, payload))
            self.assertTrue(c.encode_frame(payload)[1] & 0x80)

    def test_large_fragmented_message_and_ping_are_reassembled(self):
        body = json.dumps({'id': 1, 'result': {'v': 'x' * 100000}}).encode()
        frames = (server_frame(body[:40000], 1, False) + server_frame(b'hi', 9)
                  + server_frame(body[40000:], 0, True))
        fake, ws = socket_with(frames)
        self.assertEqual(ws.recv(), body.decode())
        pongs = [unmask(f) for f in fake.sent if not f.startswith(b'GET ')]
        self.assertIn((10, b'hi'), pongs)

    def test_handshake_accept_must_match(self):
        with self.assertRaisesRegex(c.CDPError, 'rejected'):
            socket_with(accept=False)

    def test_close_frame_is_an_error_not_a_verdict(self):
        _, ws = socket_with(server_frame(b'', 8))
        with self.assertRaises(c.CDPError):
            ws.recv()


class PolicyTests(unittest.TestCase):
    def test_only_loopback_endpoints(self):
        for value in ('http://example.com:9222', 'http://10.0.0.5:9222', 'https://127.0.0.1:9222', 'http://127.0.0.1'):
            with self.subTest(value=value), patch.dict(os.environ, {'C2C_CDP_ENDPOINT': value}):
                with self.assertRaises(ValueError):
                    c.endpoint()
        with patch.dict(os.environ, {'C2C_CDP_ENDPOINT': 'http://localhost:9333'}):
            self.assertEqual(c.endpoint(), 'http://localhost:9333')
        with self.assertRaises(ValueError):
            c.WebSocket('ws://192.168.0.2:9222/devtools/page/X', connect=lambda *a, **k: FakeSocket())

    def test_remote_websocket_from_target_list_is_refused(self):
        targets = [{'id': 'T', 'type': 'page', 'webSocketDebuggerUrl': 'ws://evil.example/devtools/page/T'}]
        with patch.object(c, 'list_targets', return_value=targets), self.assertRaises(ValueError):
            c.target('T')

    def test_missing_target_is_unavailable(self):
        with patch.object(c, 'list_targets', return_value=[]), self.assertRaises(c.CDPError):
            c.target('gone')


class FakeSession:
    calls = []
    values = []

    def __init__(self, target_id):
        self.target_id = target_id

    def call(self, method, **params):
        FakeSession.calls.append((method, params))
        return {}

    def evaluate(self, expression):
        FakeSession.calls.append(('evaluate', expression))
        return FakeSession.values.pop(0)

    def close(self):
        FakeSession.calls.append(('close', None))


class FakeModule:
    Session = FakeSession


class VerbTests(unittest.TestCase):
    def setUp(self):
        FakeSession.calls, FakeSession.values = [], []
        patcher = patch.object(w, '_cdp', return_value=FakeModule)
        patcher.start()
        self.addCleanup(patcher.stop)

    def methods(self):
        return [m for m, _ in FakeSession.calls if m not in ('evaluate', 'close')]

    def test_eval_returns_value_and_origin_atomically(self):
        FakeSession.values = [json.dumps({'origin': 'https://chatgpt.com/c/1', 'value': '"x"'})]
        self.assertEqual(w.orca('cdp:T', 'eval', '--expression', '"x"'),
                         {'result': '"x"', 'origin': 'https://chatgpt.com/c/1'})
        self.assertEqual(FakeSession.calls[-1], ('close', None))

    def test_lines_use_native_insert_and_shift_enter(self):
        FakeSession.values = ['focused']
        w.orca('cdp:T', 'fill', '--element', '@composer', '--value', 'a\n\nb')
        self.assertEqual(self.methods(), ['Input.insertText', 'Input.dispatchKeyEvent', 'Input.dispatchKeyEvent',
                                          'Input.dispatchKeyEvent', 'Input.dispatchKeyEvent', 'Input.insertText'])
        keys = [p for m, p in FakeSession.calls if m == 'Input.dispatchKeyEvent']
        self.assertTrue(all(k['modifiers'] == 8 and k['key'] == 'Enter' for k in keys))

    def test_click_requires_unique_unobscured_target(self):
        FakeSession.values = [json.dumps({'ok': False})]
        with self.assertRaisesRegex(ValueError, 'no click'):
            w.orca('cdp:T', 'click', '--element', 'form button[aria-label="Send"]')
        self.assertEqual(self.methods(), [])
        FakeSession.values = [json.dumps({'ok': True, 'x': 10, 'y': 20})]
        w.orca('cdp:T', 'click', '--element', 'form button[aria-label="Send"]')
        self.assertEqual(self.methods(), ['Input.dispatchMouseEvent'] * 3)

    def test_unknown_refs_and_keys_are_refused(self):
        for args in (('focus', '--element', '@other'), ('keypress', '--key', 'Enter'), ('snapshot',)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                w.orca('cdp:T', *args)

    def test_read_page_uses_dom_observation(self):
        turns = [{'role': 'user', 'body': 'q', 'id': 'u1'}, {'role': 'assistant', 'body': 'r', 'id': 'a1', 'complete': True}]
        obs = {'schema': w.OBSERVATION_SCHEMA, 'url': 'https://chatgpt.com/c/1', 'observedAt': 1, 'emptyDocument': False,
               'generating': False, 'posted': {'turns': turns}, 'composer': [{'text': '', 'accessibleName': 'Chat'}]}
        with patch.object(w, 'orca', return_value={'result': json.dumps(obs), 'origin': obs['url']}):
            page = w.read_page('cdp:T')
        self.assertEqual((page['page'], page['count'], page['text'], page['complete']), ('cdp:T', 1, 'r', True))
        with patch.object(w, 'orca', return_value={'result': json.dumps({'schema': 'other'})}), self.assertRaises(RuntimeError):
            w.read_page('cdp:T')

    def test_open_creates_cdp_page_and_guards_it(self):
        class Module:
            @staticmethod
            def new_target(url):
                return {'id': 'NEW'}
        with patch.object(w, '_cdp', return_value=Module):
            result = w.open_tab('https://chatgpt.com/', None, backend='cdp',
                                guard=lambda page: {'guarded': True, 'url': 'https://chatgpt.com/', 'drafts': ['']})
        self.assertEqual((result['page'], result['unknownDraft']), ('cdp:NEW', False))


if __name__ == '__main__':
    unittest.main()
