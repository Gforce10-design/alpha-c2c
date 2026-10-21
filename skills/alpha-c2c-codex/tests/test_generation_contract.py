"""Preparation correlation contracts using real producer and validator functions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
p=module('producer','c2c.py');w=module('waiter_contract','wait_reply.py')

class GenerationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.dir=Path(self.temp.name)
        self.baseline={'page':'owned','url':'https://chatgpt.com/g/g-p-test/c/test','text':'old','lastUser':'old','count':1,'userCount':1,'refs':{},'generating':False,'complete':True,'postedTurns':[]}
        self.capture=self.dir/'baseline.json';self.capture.write_text(json.dumps(self.baseline))
        self.output=self.dir/'request.txt';self.proof_path=self.dir/'proof.json'
        p.prepare_request('review','task-test','workspace-test','Korean',2,'Synthetic execution evidence.',self.capture,self.output,self.proof_path)
        self.message=self.output.read_text();self.proof=json.loads(self.proof_path.read_text())
    def current(self):
        header=' '.join(k+': '+v for k,v in self.proof['identity'].items())+' STATE: DONE'
        return {**self.baseline,'count':0,'userCount':0,'postedTurns':[{'role':'user','body':self.message,'id':'user-new','ids':['user-new']},{'role':'assistant','body':header+'\nAccepted synthetic evidence.','id':'reply-new','ids':['reply-new'],'complete':True},{'role':'user','body':'later unrelated','id':'later','ids':['later']}]}
    def receipt(self,posted=None,ident=None):
        r={'page':self.baseline['page'],'url':self.baseline['url'],'messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'attempted':True,'posted':posted,'generationProof':self.proof,'generationProofSha256':w.canonical_sha(self.proof)}
        if ident:r['userMessageId']=ident
        path=self.dir/'receipt.json';path.write_text(json.dumps(r));return path
    def test_real_preparation_binds_nonce_and_canonical_baseline(self):
        self.assertEqual(w.validated_generation(self.baseline,self.message,self.proof_path),self.proof)
        self.assertIn(self.proof['identity']['REQUEST_ID'],self.message)
        self.assertEqual(len(w.protocol_header(self.message)['REQUEST_ID']),1)
        self.assertEqual(w.validated_generation(dict(reversed(list(self.baseline.items()))),self.message,self.proof),self.proof)
    def test_nonce_is_minted_once_and_artifacts_cannot_be_overwritten(self):
        before=self.output.read_bytes(),self.proof_path.read_bytes()
        with self.assertRaises(p.InputError):p.prepare_request('review','task-test','workspace-test','Korean',2,'Changed',self.capture,self.output,self.proof_path)
        self.assertEqual(before,(self.output.read_bytes(),self.proof_path.read_bytes()))
        other=self.dir/'other.txt';side=self.dir/'other-proof.json'
        p.prepare_request('review','task-test','workspace-test','Korean',2,'Synthetic execution evidence.',self.capture,other,side)
        self.assertNotEqual(self.proof['identity']['REQUEST_ID'],json.loads(side.read_text())['identity']['REQUEST_ID'])
    def test_valid_nonlatest_associated_reply_and_stable_completion(self):
        c=self.current();self.assertTrue(w.posted(c,self.baseline,self.message,self.proof_path));self.assertTrue(w.eligible(c,self.baseline,'task-test',self.message,request_proof=self.proof_path))
        now=[0];got=w.wait_reply(self.baseline,'task-test',message=self.message,request_proof=self.proof_path,read=lambda _:c,clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n))
        self.assertEqual(got['responseMessageId'],'reply-new');self.assertEqual(now[0],20)
    def test_stale_hidden_id_difference_and_uuid_without_proof_are_not_fresh(self):
        c=self.current();self.assertFalse(w.posted(c,self.baseline,self.message));self.assertFalse(w.eligible(c,self.baseline,'task-test',self.message))
        with self.assertRaisesRegex(ValueError,'Reply admission HOLD'):w.wait_reply(self.baseline,'task-test',message=self.message,read=lambda _:c)
    def test_message_omission_never_bypasses_admission(self):
        self.assertFalse(w.eligible(self.current(),self.baseline,'task-test'))
        with self.assertRaisesRegex(ValueError,'Reply admission HOLD'):w.wait_reply(self.baseline,'task-test',read=lambda _:self.current())
    def test_each_proof_binding_rejects_tampering_before_browser_input(self):
        for field in ('schema','status','producer','producerSha256','messageSha256','baselineSha256','page','url','identity'):
            broken={**self.proof,field:'invalid'}
            with self.subTest(field=field),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer,patch.object(w,'read_page') as read:
                with self.assertRaises(ValueError):w.submit(self.baseline,self.message,self.dir/'attempt.json',request_proof=broken)
                browser.assert_not_called();composer.assert_not_called();read.assert_not_called()
        for key in ('TASK_ID','WORKSPACE_ID','ITERATION','REQUEST_ID'):
            broken={**self.proof,'identity':{**self.proof['identity'],key:'wrong'}}
            with self.subTest(key=key),self.assertRaises(ValueError):w.validated_generation(self.baseline,self.message,broken)
    def test_payload_and_baseline_edits_fail_raw_bindings(self):
        with self.assertRaises(ValueError):w.validated_generation(self.baseline,self.message+' ',self.proof)
        with self.assertRaises(ValueError):w.validated_generation({**self.baseline,'count':2},self.message,self.proof)
    def test_new_submit_without_proof_does_not_touch_composer(self):
        with patch.object(w,'read_page',return_value=self.baseline),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
            with self.assertRaisesRegex(ValueError,'Submit HOLD'):w.submit(self.baseline,self.message,self.dir/'attempt.json')
            browser.assert_not_called();composer.assert_not_called();self.assertFalse((self.dir/'attempt.json').exists())
    def test_exact_authored_presence_inhibits_input_even_unqualified(self):
        with patch.object(w,'read_page',return_value=self.current()),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
            r=w.submit(self.baseline,self.message,self.dir/'attempt.json');self.assertIsNone(r['posted']);self.assertFalse(r['attempted']);browser.assert_not_called();composer.assert_not_called()
    def test_supported_attempt_archives_proof_before_dispatch_and_repeats_observe_only(self):
        c={**self.baseline,'refs':{'box':{'role':'textbox', "name":"fixture chat composer"}}};r=self.dir/'attempt.json'
        def dispatch(*args):
            record=json.loads(r.read_text());self.assertEqual(record['generationProof'],self.proof);self.assertEqual(record['generationProofSha256'],w.canonical_sha(self.proof));raise RuntimeError('Lost dispatch acknowledgement')
        with patch.object(w,'read_page',return_value=c),patch.object(w,'composer',return_value=w.ComposerText(self.message, accessible_name="fixture chat composer")),patch.object(w,'wait_send_ready',return_value=(c,'form button[aria-label="Send"]')),patch.object(w,'orca',side_effect=dispatch) as browser:
            with self.assertRaisesRegex(RuntimeError,'Lost dispatch'):w.submit(self.baseline,self.message,r,request_proof=self.proof_path)
            self.assertEqual(browser.call_count,1)
        with patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
            result=w.confirm_posting(self.baseline,self.message,r,timeout=0,read=lambda _:self.current())
            self.assertTrue(result['posted']);browser.assert_not_called();composer.assert_not_called()
    def test_qualified_positive_pin_is_immutable(self):
        r=self.receipt(True,'original-user');result=w.confirm_posting(self.baseline,self.message,r,timeout=0,read=lambda _:self.current())
        self.assertTrue(result['posted']);self.assertEqual(result['userMessageId'],'original-user');self.assertEqual(result['lastObservation'],'REQUEST_ID_DRIFT')
        now=[0]
        with self.assertRaises(TimeoutError):w.wait_reply(self.baseline,'task-test',message=self.message,receipt=r,timeout=40,read=lambda _:self.current(),clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n))
    def test_qualified_pin_survives_absence_and_archived_proof_recovers(self):
        r=self.receipt(True,'user-new');missing=w.confirm_posting(self.baseline,self.message,r,timeout=0,read=lambda _:self.baseline);self.assertTrue(missing['posted'])
        got=w.wait_reply(self.baseline,'task-test',message=self.message,receipt=r,read=lambda _:self.current(),clock=iter([0,0,0,20]).__next__,sleep=lambda _:None)
        self.assertEqual(got['userMessageId'],'user-new')
    def test_legacy_positive_same_id_is_history_not_qualified_reply(self):
        r=self.dir/'legacy.json';r.write_text(json.dumps({'page':'owned','messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'posted':True,'userMessageId':'user-new'}))
        result=w.confirm_posting(self.baseline,self.message,r,timeout=0,read=lambda _:self.current())
        self.assertTrue(result['posted']);self.assertEqual(result['lastObservation'],'REQUEST_RENDERED');self.assertEqual(result['replyAdmission'],'HOLD_UNQUALIFIED_LEGACY')
        with self.assertRaisesRegex(ValueError,'Reply admission HOLD'):w.wait_reply(self.baseline,'task-test',message=self.message,receipt=r)
    def test_legacy_uncertain_receipt_cannot_acquire_sidecar_proof(self):
        r=self.dir/'legacy.json';r.write_text(json.dumps({'page':'owned','messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'posted':None,'attempted':True}));before=r.read_bytes()
        with patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
            with self.assertRaisesRegex(ValueError,'retrospective proof graft'):w.submit(self.baseline,self.message,r,request_proof=self.proof_path)
            self.assertEqual(r.read_bytes(),before);browser.assert_not_called();composer.assert_not_called()
    def test_archived_hash_or_replacement_proof_cannot_retarget(self):
        r=self.receipt(True,'user-new');data=json.loads(r.read_text());data['generationProofSha256']='bad';r.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'hash mismatch'):w.confirm_posting(self.baseline,self.message,r,timeout=0)
        r=self.receipt(True,'user-new')
        with self.assertRaisesRegex(ValueError,'differs'):w.confirm_posting(self.baseline,self.message,r,timeout=0,request_proof={**self.proof,'status':'invalid'})
    def test_partial_creation_preserves_failed_own_proof_and_existing_assets(self):
        output=self.dir/'missing-parent'/'new.txt';proof=self.dir/'failed-proof.json';before=self.output.read_bytes()
        with self.assertRaises(OSError):p.prepare_request('review','task-test','workspace-test','Korean',2,'new',self.capture,output,proof)
        failed=json.loads(proof.read_text());self.assertEqual(failed['status'],'creation_failed');self.assertEqual(self.output.read_bytes(),before)
        with self.assertRaises(ValueError):w.validated_generation(self.baseline,self.message,failed)
    def test_cli_all_or_none_and_stdout_compatibility(self):
        summary=self.dir/'summary.txt';summary.write_text('Synthetic summary.')
        args=['message','review','--task','task-test','--workspace-id','workspace-test','--summary-file',str(summary)]
        with patch('sys.stderr'):
            self.assertEqual(p.main(args+['--baseline',str(self.capture)]),2)
        with patch('sys.stdout') as stdout:
            self.assertEqual(p.main(args),0);self.assertIn('[C2C]',stdout.write.call_args_list[0].args[0])
        self.assertFalse((self.dir/'nonexistent.txt').exists())
