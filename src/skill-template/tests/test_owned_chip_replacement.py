import tempfile,unittest,json,sys,ast,subprocess
from pathlib import Path
from unittest.mock import patch
from test_wait_reply import w,page,MESSAGE,fixture_generation,CHAT

OLD='TASK_ID: task-27 WORKSPACE_ID: fixture-space ITERATION: 27 PHASE: REVIEW REQUEST_ID: legacy-own body'
CHIP=['fixture']
class OwnedChipBoundaryTests(unittest.TestCase):
 def ready(self):return {**page(),'refs':{'box':{'role':'textbox', "name":"fixture chat composer"}}}
 def test_exact_saved_file_and_single_chip_separator_only(self):
  self.assertTrue(w.owned_draft_matches(w.ComposerText(OLD,CHIP, accessible_name="fixture chat composer"),MESSAGE,OLD))
  self.assertTrue(w.owned_draft_matches(w.ComposerText(OLD+' ',CHIP, accessible_name="fixture chat composer"),MESSAGE,OLD))
  for value in [OLD+'  ',OLD.replace(' ','  ',1),OLD+'\n',OLD+' other']:
   self.assertFalse(w.owned_draft_matches(w.ComposerText(value,CHIP, accessible_name="fixture chat composer"),MESSAGE,OLD))
  self.assertFalse(w.owned_draft_matches(w.ComposerText(OLD+' ',['one','two'], accessible_name="fixture chat composer"),MESSAGE,OLD))
 def test_wrong_task_cannot_prove_ownership(self):
  self.assertFalse(w.owned_draft_matches(w.ComposerText(OLD,CHIP, accessible_name="fixture chat composer"),MESSAGE.replace('task-27','task-28'),OLD))
 def test_same_chat_checked_before_any_composer_focus(self):
  with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value={**self.ready(),'url':CHAT+'-other'}),patch.object(w,'composer') as composer,patch.object(w,'orca') as browser:
   with self.assertRaisesRegex(ValueError,'Conversation identity changed'):w.submit(page(),MESSAGE,Path(td)/'receipt',True,OLD,request_proof=fixture_generation(page(),MESSAGE))
   composer.assert_not_called();browser.assert_not_called()
 def test_owned_chip_requires_both_explicit_and_exact_saved_file(self):
  for explicit,known in [(False,OLD),(True,None),(True,OLD+' unknown')]:
   with self.subTest(explicit=explicit,known=known),tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=self.ready()),patch.object(w,'composer',return_value=w.ComposerText(OLD+' ',CHIP, accessible_name="fixture chat composer")),patch.object(w,'orca') as browser:
    r=Path(td)/'receipt'
    with self.assertRaises(w.ProtectedDraft):w.submit(page(),MESSAGE,r,explicit,known,request_proof=fixture_generation(page(),MESSAGE))
    self.assertFalse(r.exists());browser.assert_not_called()
 def test_owned_replace_then_original_semantic_send_once(self):
  with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=self.ready()),patch.object(w,'composer',side_effect=[w.ComposerText(OLD+' ',CHIP, accessible_name="fixture chat composer"),w.ComposerText(MESSAGE,CHIP, accessible_name="fixture chat composer")]),patch.object(w,'replace_owned_chip_draft') as replace,patch.object(w,'wait_send_ready',return_value=(self.ready(),'form button[aria-label="Send"]')),patch.object(w,'confirm_posting',return_value={'posted':True}),patch.object(w,'orca') as browser:
   self.assertTrue(w.submit(page(),MESSAGE,Path(td)/'receipt',True,OLD,request_proof=fixture_generation(page(),MESSAGE))['posted'])
   replace.assert_called_once_with(page(),MESSAGE,OLD,CHIP,'box');self.assertEqual([c.args[1] for c in browser.call_args_list],['click'])
 def test_focus_selection_readiness_failure_create_no_attempt(self):
  for failure in ['hit target','focus changed','unsafe range','insert failed']:
   with self.subTest(failure=failure),tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=self.ready()),patch.object(w,'composer',return_value=w.ComposerText(OLD+' ',CHIP, accessible_name="fixture chat composer")),patch.object(w,'replace_owned_chip_draft',side_effect=ValueError(failure)),patch.object(w,'orca') as browser:
    r=Path(td)/'receipt'
    with self.assertRaises(ValueError):w.submit(page(),MESSAGE,r,True,OLD,request_proof=fixture_generation(page(),MESSAGE))
    self.assertFalse(r.exists());browser.assert_not_called()
 def test_existing_receipt_has_no_ownership_or_input_path(self):
  with tempfile.TemporaryDirectory() as td,patch.object(w,'confirm_posting',return_value={'posted':None}) as observe,patch.object(w,'composer') as composer,patch.object(w,'replace_owned_chip_draft') as replace:
   r=Path(td)/'receipt';r.write_text('{}');w.submit(page(),MESSAGE,r,True,OLD)
   observe.assert_called_once();composer.assert_not_called();replace.assert_not_called()
 def test_hit_probe_uses_visible_intersection_and_rejects_blocked_targets(self):
  tree=ast.parse(Path(w.__file__).read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='native_focus_editor');expression=next(ast.literal_eval(n.value) for n in fn.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='hit_expression')
  for state,expected in [({'top':833,'bottom':1873},True),({'top':1500,'bottom':1873},False),({'top':833,'bottom':1873,'blocked':True},False)]:
   harness="const state="+json.dumps(state)+";const innerWidth=474,innerHeight=1224;const e={getBoundingClientRect:()=>({left:38,right:414,top:state.top,bottom:state.bottom}),contains:t=>t===e,isContentEditable:true,closest:()=>state.blocked?{}:null};const document={querySelectorAll:()=>[e],elementFromPoint:(x,y)=>x>=0&&x<innerWidth&&y>=0&&y<innerHeight?e:null};process.stdout.write(eval("+json.dumps(expression)+"));"
   result=json.loads(subprocess.run(['node','-e',harness],capture_output=True,text=True,check=True).stdout)
   self.assertEqual(result['ok'],expected)
   if expected:self.assertGreaterEqual(result['y'],833);self.assertLess(result['y'],1224)
 def test_cli_rearms_guard_when_native_replacement_raises(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);baseline=root/'baseline.json';message=root/'message.txt';proof=root/'proof.json';old=root/'old.txt';receipt=root/'receipt.json'
   baseline.write_text(json.dumps(page()));message.write_text(MESSAGE);proof.write_text(json.dumps(fixture_generation(page(),MESSAGE)));old.write_text(OLD)
   argv=['wait_reply.py','submit','--baseline',str(baseline),'--message-file',str(message),'--request-proof',str(proof),'--receipt',str(receipt),'--replace-owned-draft','--owned-draft-file',str(old)]
   with patch.object(sys,'argv',argv),patch.object(w,'read_page',return_value=self.ready()),patch.object(w,'composer',return_value=w.ComposerText(OLD+' ',CHIP, accessible_name="fixture chat composer")),patch.object(w,'replace_owned_chip_draft',side_effect=ValueError('selection failed')),patch.object(w,'guard_input') as disable,patch.object(w,'keep_guarded') as guard,patch('builtins.print'):
    self.assertEqual(w.main(),2);guard.assert_called_once_with('owned');disable.assert_called_once_with('owned',enabled=False);self.assertFalse(receipt.exists())
 def test_native_boundary_rechecks_ownership_before_any_focus(self):
  with patch.object(w,'read_page',return_value=page()),patch.object(w,'composer',return_value=w.ComposerText('unknown',CHIP, accessible_name="fixture chat composer")),patch.object(w,'orca') as browser:
   with self.assertRaises(w.ProtectedDraft):w.replace_owned_chip_draft(page(),MESSAGE,OLD,CHIP,'box')
   browser.assert_not_called()
class NativeOwnedReplacementTests(unittest.TestCase):
 def test_native_replacement_preserves_each_original_line_break(self):
  message='TASK_ID: task-27\n\nSecond line\n\n'
  actions=[]
  def browser(*args):
   actions.append(args)
   if args[1]=='eval':return {'result':json.dumps({'ok':True,'x':50,'y':90})}
   return {}
  with patch.object(w,'read_page',return_value=page()),patch.object(w,'composer',return_value=w.ComposerText(OLD+' ',CHIP, accessible_name="fixture chat composer")),patch.object(w,'orca',side_effect=browser):
   w.replace_owned_chip_draft(page(),message,OLD,CHIP,'box')
  native=[args[1:] for args in actions if args[1] in ('inserttext','keypress')]
  self.assertEqual(native,[('inserttext','--text','TASK_ID: task-27'),('keypress','--key','Shift+Enter'),('keypress','--key','Shift+Enter'),('inserttext','--text','Second line'),('keypress','--key','Shift+Enter'),('keypress','--key','Shift+Enter')])
  self.assertFalse(any(args[1] in ('fill','click') for args in actions))
 def test_changed_conversation_after_native_focus_prevents_selection_and_input(self):
  actions=[]
  def browser(*args):
   actions.append(args)
   return {'result':json.dumps({'ok':True,'x':50,'y':90})} if args[1]=='eval' else {}
  with patch.object(w,'read_page',side_effect=[page(),{**page(),'url':CHAT+'-other'}]),patch.object(w,'composer',return_value=w.ComposerText(OLD,CHIP, accessible_name="fixture chat composer")) as composer,patch.object(w,'orca',side_effect=browser):
   with self.assertRaisesRegex(ValueError,'Conversation identity changed'):w.replace_owned_chip_draft(page(),MESSAGE,OLD,CHIP,'box')
   self.assertEqual(composer.call_count,1)
  self.assertEqual([args[1] for args in actions],['eval','mouse','mouse','mouse','focus'])
 def test_actual_selection_expression_rejects_unsafe_or_unowned_range(self):
  tree=ast.parse(Path(w.__file__).read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='replace_owned_chip_draft')
  template=next(ast.literal_eval(n.value.left) for n in fn.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='selection_expression')
  expression=template % ('const withoutMentions=e=>e.read;',json.dumps(w.MENTION),json.dumps(OLD),json.dumps(CHIP))
  cases=[({},True),({'text':OLD+' '},True),({'text':OLD+' extra-after-chip'},False),({'text':OLD+'  '},False),({'inert':True},False),({'rangeText':OLD+' '},False),({'editable':'true'},False),({'names':['other']},False),({'chipCount':2},False)]
  for state,expected in cases:
   with self.subTest(state=state):
    harness=r'''const state=STATE,expected=OLD,names=CHIP;
    const chip={getAttribute:()=>state.editable??'false'};
    const e={read:{text:state.text??expected,mentions:state.names??names},querySelectorAll:()=>Array(state.chipCount??1).fill(chip)};
    const range={setStart(){},setEndBefore(){},cloneContents:()=>({querySelector:()=>state.inert?{}:null}),toString:()=>state.rangeText??expected};
    const selection={rangeCount:0,removeAllRanges(){this.rangeCount=0},addRange(){this.rangeCount=1},getRangeAt:()=>range};
    const getSelection=()=>selection;
    const document={querySelectorAll:()=>[e],createRange:()=>range};
    process.stdout.write(eval(EXPR));'''.replace('STATE',json.dumps(state)).replace('OLD',json.dumps(OLD)).replace('CHIP',json.dumps(CHIP)).replace('EXPR',json.dumps(expression))
    result=json.loads(subprocess.run(['node','-e',harness],capture_output=True,text=True,check=True).stdout)
    self.assertEqual(result['ok'],expected)


class EmptyChipBoundaryTests(unittest.TestCase):
 def test_unknown_text_blocks_before_focus_or_input(self):
  with patch.object(w,'read_page',return_value=page()),patch.object(w,'composer',return_value=w.ComposerText('stray',CHIP, accessible_name="fixture chat composer")),patch.object(w,'native_focus_editor') as focus,patch.object(w,'orca') as browser:
   with self.assertRaises(w.ProtectedDraft):w.insert_empty_chip_draft(page(),MESSAGE,CHIP,'box')
   focus.assert_not_called();browser.assert_not_called()
 def test_changed_chat_blocks_before_empty_editor_read(self):
  with patch.object(w,'read_page',return_value={**page(),'url':CHAT+'-other'}),patch.object(w,'composer') as composer,patch.object(w,'native_focus_editor') as focus:
   with self.assertRaisesRegex(ValueError,'Conversation identity changed'):w.insert_empty_chip_draft(page(),MESSAGE,CHIP,'box')
   composer.assert_not_called();focus.assert_not_called()
 def test_user_text_or_chip_change_after_native_focus_prevents_input(self):
  for changed in [w.ComposerText('stray',CHIP, accessible_name="fixture chat composer"),w.ComposerText('', ['other'], accessible_name="fixture chat composer")]:
   with self.subTest(changed=str(changed)),patch.object(w,'read_page',return_value=page()),patch.object(w,'composer',side_effect=[w.ComposerText('',CHIP, accessible_name="fixture chat composer"),changed]),patch.object(w,'native_focus_editor') as focus,patch.object(w,'orca') as browser:
    with self.assertRaises(w.ProtectedDraft):w.insert_empty_chip_draft(page(),MESSAGE,CHIP,'box')
    focus.assert_called_once_with('owned','box');browser.assert_not_called()
 def test_empty_chip_keeps_each_original_line_and_never_dispatches_send(self):
  calls=[]
  def browser(*args):calls.append(args);return {'result':'caret-start'} if args[1]=='eval' else {}
  with patch.object(w,'read_page',return_value=page()),patch.object(w,'composer',return_value=w.ComposerText(' ',CHIP, accessible_name="fixture chat composer")),patch.object(w,'native_focus_editor') as focus,patch.object(w,'orca',side_effect=browser):
   w.insert_empty_chip_draft(page(),MESSAGE,CHIP,'box')
  self.assertEqual([args[-1] for args in calls if args[1]=='inserttext'],MESSAGE.split('\n'))
  self.assertEqual(sum(args[1]=='keypress' for args in calls),MESSAGE.count('\n'))
  self.assertFalse(any(args[1] in ('fill','click') for args in calls));focus.assert_called_once_with('owned','box')

if __name__=='__main__':unittest.main()
