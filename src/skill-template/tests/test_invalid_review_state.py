"""Behavioral regression with authored DOM fixtures; no browser or Send calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

WAITER = Path(__file__).resolve().parents[1] / 'scripts/wait_reply.py'
spec = importlib.util.spec_from_file_location('waiter_under_test', WAITER)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
TASK = 'state-repair-fixture'
MESSAGE = ('TASK_ID: state-repair-fixture\nWORKSPACE_ID: fixture-space\nITERATION: 3\n'
           'REQUEST_ID: request-00000000-0000-4000-8000-000000000001\nSTATE: REVIEW_REQUEST\nReview.')
BASE = dict(page='owned', url='https://chatgpt.com/g/g-p-fixture/c/owned',
            text='old answer', lastUser='old request', userCount=1, count=1,
            complete=True, generating=False, refs={})

def header(state='EXECUTING', **changes):
    values = dict(TASK_ID=TASK, WORKSPACE_ID='fixture-space', ITERATION='3',
                  REQUEST_ID='request-00000000-0000-4000-8000-000000000001', STATE=state)
    values.update(changes)
    return '\n'.join(k + ': ' + v for k, v in values.items())

def observation(body=None, *, complete=True, generating=False, uid='user-own', aid='assistant-own', segments=None):
    body = header() + '\n\nActual completed answer.' if body is None else body
    return {**BASE, 'text': body, 'lastUser': MESSAGE, 'userCount': 2, 'count': 2,
            'complete': complete, 'generating': generating,
            'postedTurns': [dict(role='user', body=MESSAGE, id=uid, ids=[uid]),
                            dict(role='assistant', body=body, id=aid, ids=[aid],
                                 complete=complete, protocolSegments=segments if segments is not None else [body])]}

class StateRepairTests(unittest.TestCase):
    def run_wait(self, observations, *, pin=True, proof=True):
        now = [0]
        reads = []
        def read(_):
            reads.append(now[0])
            value = observations[min(len(reads)-1, len(observations)-1)]
            if isinstance(value, BaseException):
                raise value
            return value
        def sleep(duration):
            now[0] += duration
        producer = '__SKILL_NAME__' if '__SKILL_NAME__/scripts/c2c.py' in WAITER.read_text() else 'alpha-c2c-codex'
        binding = dict(schema='c2c-prepared-request-v1', status='prepared',
                       producer=producer + '/scripts/c2c.py',
                       producerSha256=hashlib.sha256(WAITER.with_name('c2c.py').read_bytes()).hexdigest(),
                       messageSha256=hashlib.sha256(MESSAGE.encode()).hexdigest(),
                       baselineSha256=w.canonical_sha(BASE), page=BASE['page'], url=BASE['url'],
                       identity={k:next(iter(v)) for k,v in w.protocol_header(MESSAGE).items() if k != 'STATE'})
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            attempt = dict(page=BASE['page'], url=BASE['url'], messageSha256=binding['messageSha256'],
                           posted=True, userMessageId='user-own', generationProof=binding,
                           generationProofSha256=w.canonical_sha(binding))
            receipt.write_text(json.dumps(attempt))
            original = receipt.read_bytes()
            with patch.object(w, 'orca') as browser:
                result = w.wait_reply(BASE, TASK, timeout=120, interval=20, read=read,
                                      sleep=sleep, clock=lambda:now[0], message=MESSAGE,
                                      receipt=receipt if pin else None, request_proof=binding if proof else None)
                browser.assert_not_called()
            self.assertEqual(receipt.read_bytes(), original)
            return result, reads

    def test_completed_invalid_states_refused_after_two_full_reads(self):
        for state in ('EXECUTING', 'IN_PROGRESS', 'UNKNOWN'):
            with self.subTest(state=state):
                result, reads = self.run_wait([observation(header(state))])
                self.assertEqual(result['type'], 'reply_invalid_state')
                self.assertEqual(result['observedState'], state)
                self.assertFalse(result['ok'])
                self.assertFalse(result['complete'])
                self.assertTrue(result['rejectedResponse']['complete'])
                self.assertEqual(reads, [0, 20])

    def test_incomplete_and_generating_reads_do_not_count(self):
        done = observation()
        result, reads = self.run_wait([observation(complete=False), observation(generating=True), done, done])
        self.assertEqual(result['type'], 'reply_invalid_state')
        self.assertEqual(reads, [0, 20, 40, 60])

    def test_corrected_plan_needs_its_own_two_stable_reads(self):
        good = observation(header('PLAN'))
        result, reads = self.run_wait([observation(), good, good])
        self.assertNotIn('type', result)
        self.assertTrue(result['complete'])
        self.assertEqual(reads, [0, 20, 40])

    def test_body_change_resets_negative_stability(self):
        changed = observation(header() + '\n\nSecond full answer.')
        result, reads = self.run_wait([observation(), changed, changed])
        self.assertEqual(result['type'], 'reply_invalid_state')
        self.assertEqual(reads, [0, 20, 40])

    def test_error_and_blank_observations_reset_stability(self):
        blank = {**BASE, 'url':'about:blank', 'text':'', 'lastUser':'', 'userCount':0,
                 'count':0, 'complete':False, 'generating':False}
        for interruption in (RuntimeError('snapshot unavailable'), blank):
            with self.subTest(interruption=type(interruption).__name__):
                result, reads = self.run_wait([observation(), interruption, observation(), observation()])
                self.assertEqual(result['type'], 'reply_invalid_state')
                self.assertEqual(reads, [0, 20, 40, 60])

    def test_ambiguous_or_quoted_headers_stay_unqualified(self):
        bad = [observation(header() + '\nSTATE: EXECUTING'),
               observation('> ' + header().replace('\n','\n> ')),
               observation(header().replace('STATE: EXECUTING','')),
               observation(segments=[header(), header()])]
        for current in bad:
            with self.subTest(body=current['text']):
                with self.assertRaises(TimeoutError):
                    self.run_wait([current])

    def test_missing_pin_or_wrong_association_cannot_refuse(self):
        with self.assertRaises(TimeoutError):
            self.run_wait([observation()], pin=False)
        with self.assertRaises(TimeoutError):
            self.run_wait([observation(uid='different-user')])

    def test_duplicate_positive_header_tokens_remain_unqualified(self):
        for key, value in (('STATE', 'PLAN'), ('TASK_ID', TASK), ('ITERATION', '3')):
            with self.subTest(key=key):
                current = observation(header('PLAN') + '\n' + key + ': ' + value)
                with self.assertRaises(TimeoutError):
                    self.run_wait([current])

    def test_identity_conflict_remains_primary_refusal(self):
        for state in ('PLAN', 'EXECUTING'):
            with self.subTest(state=state):
                result, reads = self.run_wait([observation(header(state, ITERATION='4'))])
                self.assertEqual(result['type'], 'reply_mismatch')
                self.assertIn('ITERATION', result['conflicts'])
                self.assertEqual(reads, [0, 20])

if __name__ == '__main__':
    unittest.main()
