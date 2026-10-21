import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'src/skill-template/scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

E = load('evidence')
W = load('wait_reply')

class EvidenceTests(unittest.TestCase):
    def repo(self, directory):
        root = Path(directory) / 'repo'; root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root/'source.txt').write_text('original\n')
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        (root/'source.txt').write_text('dirty baseline\n')
        return root

    def test_dirty_preserved_real_commands_and_failure_codes(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.repo(d)
            results, comparison = E.execute(root, Path(d)/'evidence', [[sys.executable, '-c', 'print("real output"); raise SystemExit(7)']])
            self.assertTrue(comparison['identical'])
            self.assertEqual(results[0]['exitCode'], 7)
            self.assertIn('real output', Path(results[0]['output']).read_text())

    def test_same_status_content_mutation_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.repo(d)
            old = E.snapshot(root)
            _, comparison = E.execute(root, Path(d)/'evidence', [[sys.executable, '-c', 'from pathlib import Path; Path("source.txt").write_text("different dirty bytes")']])
            self.assertEqual(old['status'], E.snapshot(root)['status'])
            self.assertFalse(comparison['identical'])
            self.assertIn('files', comparison['changedSections'])

    def test_branch_refs_and_raw_index_recorded_and_compared(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.repo(d)
            snap = E.snapshot(root)
            self.assertTrue(snap['branch']['symbolicRef'].startswith('refs/heads/'))
            self.assertIn(snap['head'], snap['refs'])
            index = root / '.git' / 'index'
            self.assertEqual(snap['gitIndexFile']['sha256'], hashlib.sha256(index.read_bytes()).hexdigest())
            for argv, section in ((['git', 'checkout', '-q', '-b', 'other'], 'branch'),
                                  (['git', 'branch', 'extra'], 'refs'),
                                  (['git', 'update-index', '--refresh', '-q'], None)):
                with self.subTest(argv=argv):
                    before = index.read_bytes()
                    _, comparison = E.execute(root, Path(d) / ('ev-' + argv[1]), [argv])
                    if section:
                        self.assertIn(section, comparison['changedSections'])
                    elif index.read_bytes() != before:
                        self.assertEqual(comparison['changedSections'], ['gitIndexFile'])

    def test_snapshot_never_rewrites_index_and_detached_head_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.repo(d)
            subprocess.run(['git', '-C', str(root), 'checkout', '-q', '--', 'source.txt'], check=True)
            import os, time
            future = time.time() + 5
            os.utime(root / 'source.txt', (future, future))  # Stale stat cache invites an index refresh.
            index = root / '.git' / 'index'
            before = index.read_bytes()
            first, second = E.snapshot(root), E.snapshot(root)
            self.assertEqual(index.read_bytes(), before)
            self.assertEqual(first, second)
            subprocess.run(['git', '-C', str(root), 'checkout', '-q', '--detach'], check=True)
            self.assertEqual(E.snapshot(root)['branch'], {'detached': True})

    def test_index_change_during_observation_is_not_a_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.repo(d)
            original = E.observe
            def racing(workspace, env):
                subprocess.run(['git', '-C', str(root), 'add', 'source.txt'], check=True)  # another process
                return original(workspace, env)
            with patch.object(E, 'observe', side_effect=racing):
                with self.assertRaisesRegex(RuntimeError, 'index changed while observing'):
                    E.snapshot(root)

    def test_plan_required_wrong_task_or_stage_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d)/'plan.txt'; plan.write_text('actual plan')
            session = {'ok':True, 'session':{'checkpoint':{'taskId':'task', 'iteration':2,'protocolState':'INIT'}}}
            with self.assertRaises(ValueError): E.require_plan(session, 'task', 2, plan)
            session['session']['checkpoint']['protocolState']='PLAN_RECEIVED'
            E.require_plan(session, 'task', 2, plan)
            with self.assertRaises(ValueError): E.require_plan(session, 'other', 2, plan)

    def test_record_contains_all_required_flags_and_real_code(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'output.txt'; path.write_text('actual')
            with patch.object(E.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'recorded','')) as run:
                E.record('bridge', Path(d), 'task', 2, path, 'test argv', 7, 0)
            argv = run.call_args.args[0]
            for flag,value in [('--command','test argv'),('--output-file',str(path)),('--exit-code','7'),('--exit-status','failed')]:
                self.assertEqual(argv[argv.index(flag)+1], value)
            self.assertNotIn('shell', run.call_args.kwargs)

class WaitTests(unittest.TestCase):
    # 2026-09-30 posting fix: a turn is recognized by the posted user body (counts are
    # supplementary) and a reply must differ from the baseline reply.
    # Posting, draft, timeout and receipt behaviour under the correlation-proof protocol is
    # covered by the generated skills' own tests (skills/*/tests/test_wait_reply.py).
    def page(self, **kw):
        return dict({'page':'p','url':'https://chatgpt.com/c/one','count':2,'text':'TASK_ID: task\nSTATE: DONE','complete':True,'generating':False,'userCount':2,'lastUser':'- StaticText "TASK_ID: task"'}, **kw)
    def baseline(self, **kw):
        return self.page(**{'count':1,'userCount':1,'lastUser':'- StaticText "earlier request"','text':'TASK_ID: task\nSTATE: PLAN', **kw})
    def observe_with(self, read, now):
        # confirm_posting binds read/sleep/clock defaults at definition time; inject fakes.
        original = W.confirm_posting
        def sleep(s): now[0] += s
        return patch.object(W, 'confirm_posting', side_effect=lambda b, m, r, timeout=120, protect=None: original(b, m, r, timeout=timeout, read=read, sleep=sleep, clock=lambda: now[0], protect=protect))

    def test_snapshot_extracts_only_latest_assistant_not_user(self):
        snapshot='- heading "내가 한 말:"\n- StaticText "STATE: DONE task"\n- heading "ChatGPT 답변:"\n- StaticText "STATE: PLAN task"\n- button "응답 다시 생성"\n- heading "내가 한 말:"\n- StaticText "STATE: DONE task"'
        response={'ok':True,'result':{'browserPageId':'p','origin':'https://chatgpt.com/c/one','snapshot':snapshot,'refs':{}}}
        dom={'ok':True,'result':{'origin':'https://chatgpt.com/c/one','result':json.dumps({'body':'STATE: DONE task','id':None})}}
        run=lambda *responses: patch.object(W.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0,json.dumps(r),'') for r in responses])
        with run(response,dom):
            self.assertEqual(W.read_page('p')['text'],'')
        response['result']['snapshot']+='\n- heading "ChatGPT 답변:"\n- StaticText "STATE: BLOCKED task"\n- button "응답 다시 생성"'
        with run(response,dom):
            page=W.read_page('p')
            self.assertEqual(page['count'],2); self.assertNotIn('STATE: DONE',page['text']); self.assertTrue(page['complete'])
            self.assertEqual(page['userBody'],'STATE: DONE task')

if __name__=='__main__': unittest.main()
