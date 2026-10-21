#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client for a user-launched local Chrome. No dependencies."""
import base64
import hashlib
import json
import os
import socket
import struct
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = 'http://127.0.0.1:9222'
LOOPBACK = ('127.0.0.1', 'localhost', '::1', '[::1]')
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_MESSAGE = 64 * 1024 * 1024


class CDPError(RuntimeError):
    """Browser unavailable or command failed; never a posting or reply verdict."""


def endpoint():
    value = os.environ.get('C2C_CDP_ENDPOINT', DEFAULT_ENDPOINT)
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != 'http' or parts.hostname not in LOOPBACK or not parts.port:
        raise ValueError('CDP endpoint must be http://127.0.0.1:<port>; remote browsers are refused')
    return '%s://%s' % (parts.scheme, parts.netloc)


def _http(method, path, opener=urllib.request.urlopen):
    request = urllib.request.Request(endpoint() + path, method=method)
    try:
        with opener(request, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except OSError as error:
        raise CDPError('Chrome DevTools endpoint unavailable at %s; launch Chrome as documented' % endpoint()) from error


def list_targets(opener=urllib.request.urlopen):
    return [t for t in _http('GET', '/json/list', opener) if t.get('type') == 'page']


def new_target(url, opener=urllib.request.urlopen):
    return _http('PUT', '/json/new?' + urllib.parse.quote(url, safe=':/?=&'), opener)


def target(target_id, opener=urllib.request.urlopen):
    found = [t for t in list_targets(opener) if t.get('id') == target_id]
    if len(found) != 1:
        raise CDPError('Owned Chrome tab %s is gone; retain the request and receipt' % target_id)
    ws = urllib.parse.urlsplit(found[0].get('webSocketDebuggerUrl', ''))
    if ws.scheme != 'ws' or ws.hostname not in LOOPBACK:
        raise ValueError('Refusing non-loopback DevTools websocket')
    return found[0]


def encode_frame(payload, opcode=1, mask=None):
    """Client frames are always masked (RFC 6455 section 5.3)."""
    mask = os.urandom(4) if mask is None else mask
    head = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack('>H', size)
    else:
        head += bytes([0x80 | 127]) + struct.pack('>Q', size)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebSocket:
    def __init__(self, url, connect=socket.create_connection):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'ws' or parts.hostname not in LOOPBACK:
            raise ValueError('Refusing non-loopback DevTools websocket')
        self.sock = connect((parts.hostname, parts.port or 80), timeout=45)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + ('?' + parts.query if parts.query else '')
        self.sock.sendall(('GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n'
                           % (path, parts.netloc, key)).encode())
        header = b''
        while b'\r\n\r\n' not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError('DevTools websocket closed during handshake')
            header += chunk
        header, self.buffer = header.split(b'\r\n\r\n', 1)
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        lines = header.decode('latin-1').split('\r\n')
        if lines[0].split()[1:2] != ['101'] or not any(
                l.lower().startswith('sec-websocket-accept:') and l.split(':', 1)[1].strip() == accept for l in lines):
            raise CDPError('DevTools websocket handshake rejected')

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(65536, size - len(self.buffer)))
            if not chunk:
                raise CDPError('DevTools websocket closed')
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def send(self, text):
        self.sock.sendall(encode_frame(text.encode('utf-8')))

    def recv(self):
        message, kind = b'', None
        while True:
            first, second = self._read(2)
            fin, opcode, size = first & 0x80, first & 0x0F, second & 0x7F
            if size == 126:
                size = struct.unpack('>H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            if size > MAX_MESSAGE or len(message) + size > MAX_MESSAGE:
                raise CDPError('DevTools message too large')
            payload = self._read(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 9:
                self.sock.sendall(encode_frame(payload, 10))
                continue
            if opcode == 10:
                continue
            if opcode == 8:
                raise CDPError('DevTools websocket closed by browser')
            if opcode in (1, 2):
                message, kind = payload, opcode
            elif opcode == 0 and kind is not None:
                message += payload
            else:
                raise CDPError('Unexpected websocket frame')
            if fin:
                return message.decode('utf-8')

    def close(self):
        try:
            self.sock.sendall(encode_frame(b'', 8))
        except OSError:
            pass
        self.sock.close()


class Session:
    """One short-lived connection per helper operation."""

    def __init__(self, target_id, opener=urllib.request.urlopen, socket_factory=WebSocket):
        self.ws = socket_factory(target(target_id, opener)['webSocketDebuggerUrl'])
        self.next_id = 0

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({'id': self.next_id, 'method': method, 'params': params}))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get('id') != self.next_id:
                continue  # Events are not command results.
            if 'error' in reply:
                raise CDPError('%s failed: %s' % (method, reply['error'].get('message', 'unknown')))
            return reply.get('result', {})

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=False)
        if result.get('exceptionDetails'):
            raise CDPError('Page script failed; observe again without sending')
        return result.get('result', {}).get('value')

    def close(self):
        self.ws.close()
