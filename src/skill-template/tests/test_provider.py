"""Executor provider boundaries, with real prepared-request production."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = '__EXECUTOR_ID__'
OTHER = 'codex' if EXECUTOR == 'claude' else 'claude'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load('provider_producer', 'c2c.py')
w = load('provider_waiter', 'wait_reply.py')


class ProviderTests(unittest.TestCase):
    def test_default_and_explicit_status_keep_native_transport(self):
        for extra in ([], ['--executor', EXECUTOR]):
            with self.subTest(extra=extra), patch.object(p, 'query_status', return_value={}) as query, patch('sys.stdout'):
                self.assertEqual(p.main(['status', '--workspace', str(ROOT)] + extra), 0)
                query.assert_called_once_with(ROOT, '__BRIDGE__', EXECUTOR)

    def test_status_names_the_executor_connector_and_never_the_other_one(self):
        data = {'ok': True, 'running': True, 'tokenCount': 3, 'workspaceName': 'claude-code-book'}
        self.assertEqual(p.status_summary(data)['expectedConnector'], p.expected_connector(EXECUTOR, 'claude-code-book'))
        self.assertEqual(p.status_summary(data, 'claude')['expectedConnector'], 'Code with ChatGPT · claude-code-book')
        self.assertEqual(p.status_summary(data, 'codex')['expectedConnector'], 'Codex with ChatGPT · claude-code-book')
        self.assertNotIn('expectedConnector', p.status_summary({'ok': True, 'running': True, 'tokenCount': 3}))

    def test_real_prepared_producer_cannot_be_relabelled_as_other_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = {'page': 'owned', 'url': 'https://chatgpt.com/c/synthetic', 'generating': False}
            capture, output, proof_path = (root / name for name in ('baseline.json', 'request.txt', 'proof.json'))
            capture.write_text(json.dumps(baseline), encoding='utf-8')
            p.prepare_request('review', 'fixture-task', 'fixture-space', 'English', 1,
                              'Synthetic provider evidence.', capture, output, proof_path)
            message = output.read_text(encoding='utf-8')
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            self.assertEqual(proof['producer'], '__SKILL_NAME__/scripts/c2c.py')
            self.assertEqual(w.validated_generation(baseline, message, proof), proof)
            proof['producer'] = f'alpha-c2c-{OTHER}/scripts/c2c.py'
            with patch.object(w, 'read_page') as read, patch.object(w, 'composer') as composer, patch.object(w, 'orca') as browser:
                with self.assertRaisesRegex(ValueError, 'correlation binding failed'):
                    w.submit(baseline, message, root / 'receipt.json', request_proof=proof)
                read.assert_not_called()
                composer.assert_not_called()
                browser.assert_not_called()
                self.assertFalse((root / 'receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
