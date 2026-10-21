"""Agent-driven delivery: saved DOM observations replayed through the same judges."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load('agent_mode_producer', 'c2c.py')
w = load('agent_mode_waiter', 'wait_reply.py')
PAGE = 'agent:tab-1'
URL = 'https://chatgpt.com/c/agent-fixture'
SEND = {'ready': True, 'selector': 'form button[aria-label="Send"]'}


def turn(role, body, ident, complete=False):
    return {'role': role, 'body': body, 'protocolSegments': [body], 'mentions': [], 'id': ident,
            'ids': [ident], 'key': '', 'complete': complete}


def observation(at, turns, draft='', generating=False, send=None, url=URL):
    return {'schema': w.OBSERVATION_SCHEMA, 'url': url, 'observedAt': at * 1000, 'emptyDocument': False,
            'generating': generating, 'posted': {'turns': turns},
            'composer': [{'text': draft, 'mentions': [], 'accessibleName': 'Chat with ChatGPT'}],
            'sendReady': send or {'ready': False, 'reason': 'send-disabled'}}


class AgentModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.history = [turn('user', 'earlier request', 'u1'), turn('assistant', 'earlier reply', 'a1', True)]
        self.baseline = w.agent_capture(observation(1000, self.history), PAGE)
        capture = self.write('baseline.json', self.baseline)
        self.message_path, self.proof_path = self.dir / 'request.txt', self.dir / 'proof.json'
        p.prepare_request('review', 'agent-task', 'fixture-space', 'English', 1,
                          'Synthetic agent evidence.', capture, self.message_path, self.proof_path)
        self.message = self.message_path.read_text(encoding='utf-8')
        self.proof = json.loads(self.proof_path.read_text(encoding='utf-8'))
        self.receipt = self.dir / 'receipt.json'
        header = w.protocol_header(self.message)
        ident = '\n'.join('%s: %s' % (k, next(iter(header[k]))) for k in ('TASK_ID', 'WORKSPACE_ID', 'ITERATION', 'REQUEST_ID'))
        self.reply = ident + '\nSTATE: PLAN\nThe plan.'
        self.posted = self.history + [turn('user', self.message, 'u2')]

    def write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def obs(self, name, *args, **kwargs):
        return self.write(name, observation(*args, **kwargs))

    def test_expression_is_read_only_and_carries_schema(self):
        self.assertIn(w.OBSERVATION_SCHEMA, w.OBSERVE_EXPRESSION)
        for verb in ('.click(', 'dispatchEvent', 'execCommand', '.submit('):
            self.assertNotIn(verb, w.OBSERVE_EXPRESSION)

    def test_observation_maps_to_read_page_shape(self):
        page = w.page_from_observation(observation(1, self.posted + [turn('assistant', 'r', 'a2', True)]), PAGE)
        self.assertEqual((page['count'], page['userCount'], page['text'], page['userMessageId']), (2, 2, 'r', 'u2'))
        self.assertTrue(page['complete'])
        self.assertEqual(page['refs'], {'composer': {'role': 'textbox', 'name': 'Chat with ChatGPT'}})
        pending = w.page_from_observation(observation(1, self.posted), PAGE)
        self.assertEqual(pending['text'], '')
        self.assertFalse(pending['complete'])
        with self.assertRaises(ValueError):
            w.page_from_observation(observation(1, [{'role': 'tool', 'body': 'x'}]), PAGE)

    def test_saved_string_result_is_accepted(self):
        path = self.dir / 'string.json'
        path.write_text(json.dumps(json.dumps(observation(5, self.history))), encoding='utf-8')
        self.assertEqual(w.load_observation(path)['observedAt'], 5000)
        path.write_text(json.dumps({'url': URL}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Not a'):
            w.load_observation(path)

    def test_capture_reports_unknown_draft_and_rejects_non_agent_page(self):
        self.assertTrue(w.agent_capture(observation(1, self.history, draft='mine'), PAGE)['unknownDraft'])
        with self.assertRaises(ValueError):
            w.agent_capture(observation(1, self.history), 'orca-page')
        with self.assertRaisesRegex(ValueError, 'generating'):
            w.agent_capture(observation(1, self.history, generating=True), PAGE)

    def test_orca_refuses_agent_pages(self):
        with self.assertRaisesRegex(ValueError, 'Agent-driven'):
            w.orca(PAGE, 'snapshot')

    def test_check_send_requires_exact_draft_ready_button_and_proof(self):
        cases = [(observation(1100, self.history, draft='', send=SEND), 'exact prepared message'),
                 (observation(1100, self.history, draft=self.message), 'not ready'),
                 (observation(1100, self.history, draft=self.message, send=SEND, generating=True), 'Page changed'),
                 (observation(900, self.history, draft=self.message, send=SEND), 'predates'),
                 (observation(1100, self.history, draft=self.message, send={'ready': True, 'selector': 'button'}), 'not ready'),
                 (observation(1100, self.history, draft=self.message, send=SEND, url='https://chatgpt.com/c/other'), 'Conversation identity')]
        for obs, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                w.agent_check_send(self.baseline, self.message, self.receipt, obs, self.proof)
            self.assertFalse(self.receipt.exists())
        with self.assertRaisesRegex(ValueError, 'HOLD'):
            w.agent_check_send(self.baseline, self.message, self.receipt,
                               observation(1100, self.history, draft=self.message, send=SEND), None)

    def test_full_agent_round_posts_once_and_waits_two_paced_reads(self):
        ready = observation(1100, self.history, draft=self.message, send=SEND)
        result = w.agent_check_send(self.baseline, self.message, self.receipt, ready, self.proof)
        self.assertEqual((result['status'], result['selector']), ('READY_TO_CLICK', SEND['selector']))
        self.assertEqual(json.loads(self.receipt.read_text())['status'], 'ATTEMPTED')
        with self.assertRaisesRegex(ValueError, 'never click again'):
            w.agent_check_send(self.baseline, self.message, self.receipt, ready, self.proof)
        # The click happened in the agent's browser; nothing rendered yet.
        first = self.obs('c1.json', 1110, self.history)
        self.assertEqual(w.agent_confirm(self.baseline, self.message, self.receipt, [first], 120, self.proof)['status'], 'PENDING')
        second = self.obs('c2.json', 1120, self.posted, generating=True)
        confirmed = w.agent_confirm(self.baseline, self.message, self.receipt, [first, second], 120, self.proof)
        self.assertEqual((confirmed['status'], confirmed['userMessageId']), ('POSTED', 'u2'))
        done = self.posted + [turn('assistant', self.reply, 'a2', True)]
        r1 = self.obs('r1.json', 1140, done)
        pending = w.agent_wait(self.baseline, 'agent-task', self.message, self.receipt, [r1], 600, self.proof)
        self.assertEqual(pending['status'], 'PENDING')
        r2 = self.obs('r2.json', 1160, done)
        reply = w.agent_wait(self.baseline, 'agent-task', self.message, self.receipt, [r1, r2], 600, self.proof)
        self.assertEqual((reply['userMessageId'], reply['responseMessageId']), ('u2', 'a2'))

    def test_same_or_rushed_observation_is_not_a_second_stable_read(self):
        done = self.posted + [turn('assistant', self.reply, 'a2', True)]
        r1 = self.obs('r1.json', 1140, done)
        rushed = self.obs('r2.json', 1145, done)
        for paths in ([r1, r1], [r1, rushed]):
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, 'apart'):
                w.agent_wait(self.baseline, 'agent-task', self.message, None, paths, 600, self.proof)

    def test_streaming_or_wrong_identity_reply_never_completes(self):
        streaming = self.posted + [turn('assistant', self.reply, 'a2', False)]
        paths = [self.obs('s%d.json' % i, 1140 + 20 * i, streaming, generating=True) for i in range(3)]
        self.assertEqual(w.agent_wait(self.baseline, 'agent-task', self.message, None, paths, 600, self.proof)['status'], 'PENDING')
        # An explicit identity conflict is refused once the receipt pins the posted request.
        w.agent_check_send(self.baseline, self.message, self.receipt,
                           observation(1100, self.history, draft=self.message, send=SEND), self.proof)
        w.agent_confirm(self.baseline, self.message, self.receipt, [self.obs('c.json', 1120, self.posted)], 120, self.proof)
        other = self.reply.replace('ITERATION: 1', 'ITERATION: 2')
        wrong = self.posted + [turn('assistant', other, 'a2', True)]
        paths = [self.obs('x%d.json' % i, 1140 + 20 * i, wrong) for i in range(2)]
        result = w.agent_wait(self.baseline, 'agent-task', self.message, self.receipt, paths, 600, self.proof)
        self.assertEqual(result['type'], 'reply_mismatch')

    def test_pending_past_timeout_is_uncertain_not_unposted(self):
        paths = [self.obs('t%d.json' % i, 1140 + 20 * i, self.posted) for i in range(4)]
        with self.assertRaisesRegex(TimeoutError, 'Do not resend'):
            w.agent_wait(self.baseline, 'agent-task', self.message, None, paths, 40, self.proof)

    def test_cli_round_trip_uses_exit_codes(self):
        script = ROOT / 'scripts' / 'wait_reply.py'
        run = lambda *a: subprocess.run([sys.executable, str(script), *map(str, a)], capture_output=True, text=True)
        expression = run('observe-expression')
        self.assertEqual(expression.returncode, 0)
        self.assertIn(w.OBSERVATION_SCHEMA, expression.stdout)
        base = self.dir / 'baseline.json'
        ready = self.obs('ready.json', 1100, self.history, draft=self.message, send=SEND)
        out = run('agent-check-send', '--baseline', base, '--message-file', self.message_path,
                  '--receipt', self.receipt, '--request-proof', self.proof_path, '--observation', ready)
        self.assertEqual(out.returncode, 0, out.stdout)
        first = self.obs('c1.json', 1110, self.history)
        out = run('agent-confirm', '--baseline', base, '--message-file', self.message_path,
                  '--receipt', self.receipt, '--request-proof', self.proof_path, '--observation', first)
        self.assertEqual((out.returncode, json.loads(out.stdout)['status']), (3, 'PENDING'))


if __name__ == '__main__':
    unittest.main()
