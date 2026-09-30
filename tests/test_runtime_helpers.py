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
    def page(self, **kw):
        return dict({'page':'p','url':'https://chatgpt.com/c/one','count':2,'text':'TASK_ID: task\nSTATE: DONE','complete':True,'generating':False,'userCount':2}, **kw)
    def baseline(self): return self.page(count=1,userCount=1)

    def test_old_or_streaming_or_other_task_never_complete(self):
        for p in [self.page(count=1), self.page(generating=True), self.page(complete=False), self.page(text='STATE: DONE other')]:
            self.assertFalse(W.eligible(p,self.baseline(),'task'))
        with self.assertRaises(ValueError): W.eligible(self.page(url='https://chatgpt.com/c/other'),self.baseline(),'task')

    def test_paced_polls_require_two_stable_completed_reads(self):
        now=[0]; reads=[]; sleeps=[]
        pages=[self.page(generating=True),self.page(),self.page()]
        def read(page): reads.append(page); return pages.pop(0)
        def sleep(s): sleeps.append(s); now[0]+=s
        result=W.wait_reply(self.baseline(),'task',read=read,sleep=sleep,clock=lambda:now[0])
        self.assertEqual(result['text'],'TASK_ID: task\nSTATE: DONE')
        self.assertEqual(sleeps,[20,20]); self.assertEqual(reads,['p','p','p'])

    def test_timeout_no_send_and_transient_recovery(self):
        now=[0]
        def sleep(s): now[0]+=s
        with self.assertRaises(TimeoutError):
            W.wait_reply(self.baseline(),'task',timeout=40,read=lambda p:self.page(count=1),sleep=sleep,clock=lambda:now[0])
        self.assertEqual(now[0],40)
        with self.assertRaises(RuntimeError):
            W.wait_reply(self.baseline(),'task',read=lambda p:(_ for _ in ()).throw(RuntimeError()),sleep=sleep,clock=lambda:now[0])

    def test_unposted_composer_fails_fast(self):
        now=[0]
        def sleep(s): now[0]+=s
        with self.assertRaisesRegex(ValueError, 'not posted'):
            W.wait_reply(self.baseline(),'task',read=lambda p:self.page(count=1,userCount=1),sleep=sleep,clock=lambda:now[0])
        self.assertEqual(now[0],20)

    def test_posted_requires_new_user_turn_and_actual_message(self):
        current=self.page(lastUser='- StaticText "task hello"')
        self.assertTrue(W.posted(current,self.baseline(),'task hello'))
        self.assertFalse(W.posted(current,self.baseline(),'task other'))
        self.assertFalse(W.posted(self.page(userCount=1,lastUser='- StaticText "task hello"'),self.baseline(),'task hello'))

    def test_unknown_draft_cannot_be_replaced_even_with_flag(self):
        with tempfile.TemporaryDirectory() as d:
            before=self.page(count=1,userCount=1,lastUser='',refs={'e1':{'role':'textbox'}})
            with patch.object(W,'read_page',return_value=before), patch.object(W,'composer',return_value='. 멈추지 '), patch.object(W,'orca') as api:
                with self.assertRaisesRegex(ValueError,'Different draft protected'):
                    W.submit(self.baseline(),'TASK_ID: task\nnew request',Path(d)/'receipt',True,'. 멈추지 ')
                api.assert_not_called()
        self.assertFalse(W.owned_draft_matches('TASK_ID: other\nold','TASK_ID: task\nnew','TASK_ID: other\nold'))
        self.assertFalse(W.owned_draft_matches('TASK_ID: task\nmodified','TASK_ID: task\nnew','TASK_ID: task\nold'))
        self.assertTrue(W.owned_draft_matches('TASK_ID: task\nold','TASK_ID: task\nnew','TASK_ID: task\nold'))

    def test_submit_clicks_send_and_confirms_new_user_turn(self):
        with tempfile.TemporaryDirectory() as d:
            before=self.page(count=1,userCount=1,lastUser='',refs={'e1':{'role':'textbox'},'e2':{'role':'button','name':'보내기'}})
            after=self.page(lastUser='- StaticText "task hello"')
            with patch.object(W,'read_page',side_effect=[before,before,after]), patch.object(W,'composer',side_effect=['','task hello']), patch.object(W,'orca') as api:
                result=W.submit(self.baseline(),'task hello',Path(d)/'receipt')
                self.assertTrue(result['posted'])
                self.assertEqual(api.call_args_list[-1].args,('p','click','--element','@e2'))
                self.assertEqual(len(api.call_args_list),2)

    def test_submit_uncertain_receipt_never_clicks_again(self):
        with tempfile.TemporaryDirectory() as d:
            receipt=Path(d)/'receipt'; receipt.write_text('{}')
            with patch.object(W,'read_page',return_value=self.page(count=1,userCount=1,lastUser='')), patch.object(W,'orca') as api:
                with self.assertRaisesRegex(ValueError,'Prior submit'):
                    W.submit(self.baseline(),'task hello',receipt)
                api.assert_not_called()

    def test_snapshot_extracts_only_latest_assistant_not_user(self):
        snapshot='- heading "내가 한 말:"\n- StaticText "STATE: DONE task"\n- heading "ChatGPT 답변:"\n- StaticText "STATE: PLAN task"\n- button "응답 다시 생성"\n- heading "내가 한 말:"\n- StaticText "STATE: DONE task"'
        response={'ok':True,'result':{'browserPageId':'p','origin':'https://chatgpt.com/c/one','snapshot':snapshot,'refs':{}}}
        with patch.object(W.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(response),'')):
            self.assertEqual(W.read_page('p')['text'],'')
        response['result']['snapshot']+='\n- heading "ChatGPT 답변:"\n- StaticText "STATE: BLOCKED task"\n- button "응답 다시 생성"'
        with patch.object(W.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(response),'')):
            page=W.read_page('p')
            self.assertEqual(page['count'],2); self.assertNotIn('STATE: DONE',page['text']); self.assertTrue(page['complete'])

if __name__=='__main__': unittest.main()
