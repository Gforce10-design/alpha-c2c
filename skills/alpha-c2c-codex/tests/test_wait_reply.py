"""Delivery regressions; fake snapshots and clocks, never browser writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("waiter", Path(__file__).resolve().parents[1] / "scripts/wait_reply.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
TASK = "task-27"
MESSAGE = "TASK_ID: task-27\nITERATION: 27\nSTATE: PLAN_REQUEST\nWORKSPACE_ID: fixture-space\nREQUEST_ID: request-00000000-0000-4000-8000-000000000001\nPlease plan."
PROJECT = "https://chatgpt.com/g/g-p-abc/project"
CHAT = "https://chatgpt.com/g/g-p-abc-name/c/chat1"


def fixture_generation(baseline, message):
    """Synthetic producer-format record: validate real bindings, no mocked defense.

    End-to-end generation and exclusive files are tested separately with c2c.py.
    """
    fields=w.protocol_header(message)
    return dict(schema='c2c-prepared-request-v1',status='prepared',producer='alpha-c2c-codex/scripts/c2c.py',
                producerSha256=hashlib.sha256((Path(w.__file__).with_name('c2c.py')).read_bytes()).hexdigest(),
                messageSha256=hashlib.sha256(message.encode()).hexdigest(),baselineSha256=w.canonical_sha(baseline),
                page=baseline['page'],url=baseline['url'],identity={k:next(iter(fields[k])) for k in ('TASK_ID','WORKSPACE_ID','ITERATION','REQUEST_ID')})


def text(value):
    return "- StaticText " + json.dumps(value)


def page(user="old request", reply="old answer", url=CHAT, users=5, count=5, complete=True, generating=False):
    return dict(page="owned", url=url, userCount=users, count=count, lastUser=text(user) if user else "",
                text=text(reply) if reply else "", complete=complete, generating=generating, refs={})


def correlated_receipt(record, baseline, message):
    proof=fixture_generation(baseline,message)
    return {**record,'generationProof':proof,'generationProofSha256':w.canonical_sha(proof)}


def supported_wait_fixture(current, message):
    """Upgrade synthetic AX timing fixtures with explicit authored stable units.

    This changes test inputs, not browser/eligibility functions. No legacy
    production observation receives a fabricated correlation or DOM identity.
    """
    import re
    user=w.user_body(current)
    raw=current['text']
    reply=w.visible_text(raw)
    expected=w.protocol_header(message)
    state=re.search(r'\bSTATE:\s*(PLAN|REVISE|DONE|BLOCKED|IN_PROGRESS)',reply)
    header={k:next(iter(v)) for k,v in expected.items() if k in ('TASK_ID','WORKSPACE_ID','ITERATION','REQUEST_ID')}
    for k in header:
        actual=re.search(r'\b'+k+r':\s*([A-Za-z0-9_.:-]+)',reply)
        if actual:header[k]=actual[1]
    header['STATE']=state[1] if state else 'IN_PROGRESS'
    body=' '.join(k+': '+v for k,v in header.items())+'\n\n'
    body+=re.sub(r'StaticText ("(?:[^"\\]|\\.)*")',lambda m:json.loads(m[1]),raw)
    uid=current.get('userMessageId') or 'user-'+hashlib.sha256(user.encode()).hexdigest()
    aid='assistant-'+hashlib.sha256(w.response_body(raw).encode()).hexdigest()
    return {**current,'postedTurns':[dict(role='user',body=user,id=uid,ids=[uid]),
                                   dict(role='assistant',body=body,id=aid,ids=[aid],complete=current['complete'])]}


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.baseline = page()
        self.posted = page(MESSAGE, "TASK_ID: task-27\nSTATE: PLAN")

    def receipt(self, path):
        path.write_text(json.dumps(dict(attempted=True, page="owned", messageSha256=hashlib.sha256(MESSAGE.encode()).hexdigest())))

    def test_virtualized_counts_do_not_hide_posted_request(self):
        self.assertFalse(w.posted(self.posted, self.baseline, MESSAGE))

    def test_decreasing_counts_still_match_changed_exact_user_body(self):
        current = {**self.posted, "userCount": 3, "count": 3}
        self.assertFalse(w.posted(current, self.baseline, MESSAGE))
        self.assertFalse(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_quoted_request_or_composer_is_not_posting(self):
        current = page("Someone quoted: " + MESSAGE)
        self.assertFalse(w.posted(current, self.baseline, MESSAGE))
        self.assertFalse(w.posted(self.baseline, self.baseline, MESSAGE))

    def test_same_previous_request_and_fixed_counts_is_ambiguous(self):
        self.assertFalse(w.posted(self.posted, self.posted, MESSAGE))

    def test_markdown_split_state_nodes_and_fixed_counts_complete(self):
        current = {**self.posted, "text": text("TASK_ID: task-27") + "\n" + text("STATE:") + "\n" + text("DONE")}
        self.assertFalse(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_completed_revise_is_received_after_two_stable_reads(self):
        current = page(MESSAGE, "TASK_ID: task-27\nSTATE: REVISE\nCorrect the location reference.")
        clock = Clock()
        reads = []
        def read(_):
            reads.append(clock.now)
            return current
        result = w.wait_reply(self.baseline, TASK, timeout=60, read=lambda owned: supported_wait_fixture((read)(owned), MESSAGE),
                              sleep=clock.sleep, clock=clock, message=MESSAGE, request_proof=fixture_generation(self.baseline, MESSAGE))
        self.assertEqual(w.user_body(result), MESSAGE)
        self.assertEqual(reads, [0, 20])

    def test_revise_keeps_request_task_and_completion_checks(self):
        current = page(MESSAGE, "TASK_ID: task-27\nSTATE: REVISE")
        for changed in [dict(generating=True), dict(complete=False),
                        dict(lastUser=text(MESSAGE + " different request")),
                        dict(text=text("TASK_ID: task-270\nSTATE: REVISE")),
                        dict(text=text("TASK_ID: task-27\nSTATE: IN_PROGRESS"))]:
            with self.subTest(changed=changed):
                self.assertFalse(w.eligible({**current, **changed}, self.baseline, TASK, MESSAGE))
        self.assertFalse(w.eligible(current, current, TASK, MESSAGE))

    def test_request_markers_do_not_make_old_assistant_eligible(self):
        current = {**self.baseline, "lastUser": text(MESSAGE)}
        self.assertFalse(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_response_without_new_associated_request_is_rejected(self):
        current = {**self.posted, "lastUser": self.baseline["lastUser"]}
        self.assertFalse(w.eligible(current, self.baseline, TASK))

    def test_exact_message_rejects_different_request_with_same_task(self):
        current = {**self.posted, "lastUser": text(MESSAGE.replace("27\nSTATE", "28\nSTATE"))}
        self.assertFalse(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_delayed_project_url_and_posting_after_old_window(self):
        baseline = page("", "", PROJECT, users=0, count=0)
        pending = {**baseline, "complete": False}
        clock = Clock()
        def read(_):
            return pending if clock.now < 80 else page(MESSAGE, "", CHAT, users=1, count=0, complete=False)
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "receipt.json"
            self.receipt(receipt)
            result = w.confirm_posting(baseline, MESSAGE, receipt, timeout=120, read=read, clock=clock, sleep=clock.sleep)
            self.assertIsNone(result["posted"])
            self.assertEqual(clock.now, 120)
            self.assertEqual(result["url"], CHAT)

    def test_timeout_is_unknown_and_late_reconciliation_does_not_send(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "receipt.json"
            self.receipt(receipt)
            result = w.confirm_posting(self.baseline, MESSAGE, receipt, timeout=40, read=lambda _: self.baseline, clock=clock, sleep=clock.sleep)
            self.assertIsNone(result["posted"])
            self.assertEqual(result["status"], "POSTING_UNCONFIRMED")
            with patch.object(w, "orca") as browser:
                result = w.confirm_posting(self.baseline, MESSAGE, receipt, timeout=0, read=lambda _: self.posted)
                browser.assert_not_called()
                self.assertIsNone(result["posted"])
                self.assertEqual(json.loads(receipt.read_text())["status"], "POSTING_UNCONFIRMED")

    def test_wait_survives_unchanged_counts_before_delayed_reply(self):
        clock = Clock()
        reads = []
        def read(_):
            reads.append(clock.now)
            return self.baseline if clock.now < 80 else self.posted
        result = w.wait_reply(self.baseline, TASK, timeout=140, read=lambda owned: supported_wait_fixture((read)(owned), MESSAGE), sleep=clock.sleep, clock=clock, message=MESSAGE, request_proof=fixture_generation(self.baseline, MESSAGE))
        self.assertEqual(w.user_body(result), MESSAGE)
        self.assertEqual(reads[-2:], [80, 100])

    def test_wait_timeout_never_asserts_not_posted(self):
        clock = Clock()
        with self.assertRaisesRegex(TimeoutError, "unconfirmed/pending"):
            w.wait_reply(self.baseline, TASK, timeout=40, read=lambda owned: supported_wait_fixture((lambda _: self.baseline)(owned), MESSAGE), sleep=clock.sleep, clock=clock, request_proof=fixture_generation(self.baseline, MESSAGE), message=MESSAGE)

    def test_page_origin_project_and_existing_conversation_changes_rejected(self):
        for changed in [dict(page="other"), dict(url=CHAT.replace("chatgpt.com", "example.com")), dict(url=CHAT.replace("chat1", "chat2"))]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                w.posted({**self.posted, **changed}, self.baseline, MESSAGE)
        with self.assertRaises(ValueError):
            w.posted({**self.posted, "url": CHAT.replace("abc-name", "def-name")}, page(url=PROJECT), MESSAGE)

    def test_same_page_blank_recovers_and_requires_two_new_stable_reads(self):
        blank = page('', '', 'about:blank', users=0, count=0, complete=False)
        seq = iter([self.posted, blank, blank, blank, blank, self.posted, self.posted])
        clock = Clock()
        with patch.object(w, 'orca') as browser:
            result = w.wait_reply(self.baseline, TASK, timeout=200, read=lambda owned: supported_wait_fixture((lambda _: next(seq))(owned), MESSAGE),
                                  sleep=clock.sleep, clock=clock, message=MESSAGE, request_proof=fixture_generation(self.baseline, MESSAGE))
            browser.assert_not_called()
        self.assertEqual(w.user_body(result), MESSAGE)
        self.assertEqual(clock.now, 120)

    def test_blank_posting_timeout_saves_uncertainty_then_recovers_no_send(self):
        blank = page('', '', 'about:blank', users=0, count=0, complete=False)
        clock = Clock()
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / 'receipt.json'
            self.receipt(receipt)
            with patch.object(w, 'orca') as browser:
                result = w.confirm_posting(self.baseline, MESSAGE, receipt, timeout=80,
                                          read=lambda _: blank, sleep=clock.sleep, clock=clock)
                self.assertEqual(result['status'], 'POSTING_UNCONFIRMED')
                self.assertIsNone(result['posted'])
                self.assertEqual(result['url'], CHAT)
                self.assertEqual(result['lastObservation'], 'TRANSIENT_EMPTY_PAGE')
                result = w.confirm_posting(self.baseline, MESSAGE, receipt, timeout=0,
                                          read=lambda _: self.posted)
                self.assertIsNone(result['posted'])
                browser.assert_not_called()

    def test_blank_does_not_mask_real_page_origin_or_conversation_change(self):
        blank = page('', '', 'about:blank', users=0, count=0, complete=False)
        for changed in [{**blank, 'page':'other'}, {**blank, 'url':'https://example.com'},
                        {**blank, 'url':CHAT.replace('chat1','chat2')},
                        {**blank, 'text':text('unexpected document')}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                w.check_identity(changed, self.baseline)

    def test_blank_wait_timeout_is_pending_not_completed(self):
        blank = page('', '', '', users=0, count=0, complete=False)
        clock = Clock()
        with self.assertRaises(TimeoutError):
            w.wait_reply(self.baseline, TASK, timeout=80, read=lambda owned: supported_wait_fixture((lambda _: blank)(owned), MESSAGE),
                         sleep=clock.sleep, clock=clock, message=MESSAGE, request_proof=fixture_generation(self.baseline, MESSAGE))

    def test_existing_receipt_submit_never_pre_reads_or_mutates_blank_page(self):
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td)/'receipt.json'
            self.receipt(receipt)
            with patch.object(w,'read_page') as read, patch.object(w,'orca') as browser, \
                 patch.object(w,'confirm_posting',return_value={'posted':None}) as confirm:
                self.assertIsNone(w.submit(self.baseline,MESSAGE,receipt)['posted'])
                read.assert_not_called()
                browser.assert_not_called()
                confirm.assert_called_once()

    def test_same_url_empty_snapshot_is_transient_but_new_project_composer_is_not(self):
        blank = {**page('', '', CHAT, users=0, count=0, complete=False), 'emptyDocument':True}
        with self.assertRaises(w.TransientEmptyPage):
            w.check_identity(blank,self.baseline)
        w.check_identity({**blank,'url':PROJECT,'refs':{'box':{'role':'textbox', "name":"fixture chat composer"}}},page(url=PROJECT))

    def test_acquired_new_conversation_is_pinned_during_wait(self):
        baseline = page("", "", PROJECT, users=0, count=0)
        seq = iter([self.posted, {**self.posted, "url": CHAT.replace("chat1", "chat2")}])
        clock = Clock()
        with self.assertRaisesRegex(ValueError, "Conversation identity changed"):
            w.wait_reply(baseline, TASK, read=lambda owned: supported_wait_fixture((lambda _: next(seq))(owned), MESSAGE), sleep=clock.sleep, clock=clock, message=MESSAGE, request_proof=fixture_generation(baseline, MESSAGE))

    def test_receipt_mismatch_cannot_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "receipt.json"
            self.receipt(receipt)
            with self.assertRaisesRegex(ValueError, "different page or request"):
                w.confirm_posting(self.baseline, MESSAGE + " other", receipt, timeout=0, read=lambda _: self.posted)

    def test_unknown_short_draft_preserved_without_browser_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=self.baseline), patch.object(w, "composer", return_value="ㅇㅓㅂㅅㅇ"), patch.object(w, "orca") as browser:
                with self.assertRaisesRegex(ValueError, "Different draft protected"):
                    w.submit(self.baseline, MESSAGE, Path(td) / "receipt.json", request_proof=fixture_generation(self.baseline, MESSAGE))
                browser.assert_not_called()

    def test_existing_uncertain_receipt_submit_only_observes(self):
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "receipt.json"
            self.receipt(receipt)
            with patch.object(w, "read_page", return_value=self.baseline), patch.object(w, "confirm_posting", return_value={"posted": None}) as observe, patch.object(w, "orca") as browser, patch.object(w, "composer") as composer:
                result = w.submit(self.baseline, MESSAGE, receipt)
                self.assertIsNone(result["posted"])
                observe.assert_called_once()
                browser.assert_not_called()
                composer.assert_not_called()

    def test_tables_are_part_of_response_stability(self):
        first = {**self.posted, "text": self.posted["text"] + '\n- cell "old" [ref=e11]'}
        second = {**self.posted, "text": self.posted["text"] + '\n- cell "updated" [ref=e12]'}
        third = {**second, "text": second["text"].replace("ref=e12", "ref=e99")}
        seq = iter([first, second, third])
        clock = Clock()
        result = w.wait_reply(self.baseline, TASK, read=lambda owned: supported_wait_fixture((lambda _: next(seq))(owned), MESSAGE), clock=clock, sleep=clock.sleep, message=MESSAGE, request_proof=fixture_generation(self.baseline, MESSAGE))
        self.assertIn("updated", result["text"])
        self.assertEqual(clock.now, 40)

    def test_task_id_prefix_is_not_identity(self):
        current = {**self.posted, "text": text("TASK_ID: task-270 STATE: DONE")}
        self.assertFalse(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_dom_body_restores_inert_links_and_excludes_controls(self):
        message = MESSAGE + "\nReference: https://example.com/design"
        current = {**self.posted, "userBody": message,
                   "lastUser": text(MESSAGE + "\nReference: ") + '\n- button "More"\n' + text("More")}
        self.assertFalse(w.posted(current, self.baseline, message))
        self.assertFalse(w.posted({**current, "userBody": None}, self.baseline, message))

    def test_toolbar_changes_are_not_new_user_messages(self):
        current = {**self.baseline, "lastUser": self.baseline["lastUser"] + '\n- button "More"\n' + text("More")}
        self.assertFalse(w.new_user_turn(current, self.baseline))

    def test_dom_user_identity_disambiguates_identical_virtualized_requests(self):
        before = {**self.posted, "userBody": MESSAGE, "userMessageId": "turn1"}
        current = {**before, "userMessageId": "turn2"}
        self.assertFalse(w.posted(current, before, MESSAGE))

    def test_read_page_uses_posted_user_dom_and_rejects_navigation_race(self):
        snapshot = {"browserPageId": "owned", "origin": CHAT,
                    "snapshot": '- heading "You said:"\n' + text(MESSAGE) + '\n- heading "ChatGPT said:"\n' + text("TASK_ID: task-27 STATE: PLAN") + '\n- button "Regenerate"', "refs": {}}
        dom = {"origin": CHAT, "result": json.dumps({"body": MESSAGE + " https://example.com", "id": "turn1"})}
        with patch.object(w, "orca", side_effect=[snapshot, dom]) as browser:
            current = w.read_page("owned")
            self.assertEqual(current["userBody"], MESSAGE + " https://example.com")
            self.assertEqual(current["userMessageId"], "turn1")
            self.assertEqual(browser.call_args_list[1].args[1], "eval")
        with patch.object(w, "orca", side_effect=[snapshot, {**dom, "origin": CHAT + "-other"}]):
            with self.assertRaisesRegex(RuntimeError, "Page changed during"):
                w.read_page("owned")

    def test_legacy_baseline_dom_extraction_improvement_is_not_new_turn(self):
        old = {**self.posted, "lastUser": text(MESSAGE + " Reference: ")}
        current = {**old, "userBody": MESSAGE + " Reference: https://example.com"}
        self.assertFalse(w.new_user_turn(current, old))

    def test_same_message_id_rejects_count_or_content_only_changes(self):
        old = {**self.posted, "userMessageId": "turn1"}
        current = {**old, "userCount": 6, "userBody": MESSAGE + " edited"}
        self.assertFalse(w.new_user_turn(current, old))

    def test_mixed_rich_and_plain_dom_chooses_latest_user_bubble(self):
        harness = """const old={isConnected:true,innerText:'ITERATION: 27'};const latest={isConnected:true,innerText:'ITERATION: 28'};
        const bubble=(body)=>({querySelector:()=>body,closest:()=>null});
        const document={querySelectorAll:(selector)=>selector==='[data-user-message-bubble]'?[bubble(old),bubble(latest)]:[old]};
        process.stdout.write(eval(EXPRESSION));""".replace('EXPRESSION', json.dumps(w.POSTED_USER_EXPRESSION))
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['body'], 'ITERATION: 28')

    def test_stale_dom_user_cannot_override_latest_snapshot(self):
        snapshot = {"browserPageId": "owned", "origin": CHAT,
                    "snapshot": '- heading "You said:"\n' + text(MESSAGE) + '\n- heading "ChatGPT said:"\n' + text("TASK_ID: task-27 STATE: PLAN"), "refs": {}}
        dom = {"origin": CHAT, "result": json.dumps({"body": MESSAGE.replace('ITERATION: 27', 'ITERATION: 26'), "id": None})}
        with patch.object(w, "orca", side_effect=[snapshot, dom]):
            with self.assertRaisesRegex(RuntimeError, 'DOM/snapshot identity mismatch'):
                w.read_page('owned')

    def test_no_inflight_or_unstable_response_completes(self):
        self.assertFalse(w.eligible({**self.posted, "generating": True}, self.baseline, TASK, MESSAGE))
        self.assertFalse(w.eligible({**self.posted, "complete": False}, self.baseline, TASK, MESSAGE))
        clock = Clock()
        seq = iter([self.posted, {**self.posted, "text": text("TASK_ID: task-27 STATE: PLAN updated")}, {**self.posted, "text": text("TASK_ID: task-27 STATE: PLAN updated")}])
        result = w.wait_reply(self.baseline, TASK, read=lambda owned: supported_wait_fixture((lambda _: next(seq))(owned), MESSAGE), clock=clock, sleep=clock.sleep, message=MESSAGE, request_proof=fixture_generation(self.baseline, MESSAGE))
        self.assertIn("updated", result["text"])
        self.assertEqual(clock.now, 40)


class InputGuardTests(unittest.TestCase):
    """Stray real keystrokes must not become drafts in owned ChatGPT tabs."""

    def created(self, page_id="fresh"):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps({"ok": True, "result": {"browserPageId": page_id}}), "")

    def test_open_guards_as_soon_as_composer_exists(self):
        clock = Clock()
        states = iter([RuntimeError("loading"), {"guarded": True, "drafts": []}, {"guarded": True, "url": "https://chatgpt.com/", "drafts": [""]}])
        def guard(page):
            self.assertEqual(page, "fresh")
            state = next(states)
            if isinstance(state, Exception):
                raise state
            return state
        result = w.open_tab("https://chatgpt.com/", "path:/x", run=self.created(), guard=guard, sleep=clock.sleep, clock=clock)
        self.assertEqual(result, {"page": "fresh", "url": "https://chatgpt.com/", "guarded": True, "draftAtOpen": "", "unknownDraft": False})

    def test_open_reports_preexisting_draft_verbatim_without_clearing(self):
        result = w.open_tab("u", "w", run=self.created(), guard=lambda _: {"guarded": True, "drafts": ["r"]})
        self.assertTrue(result["unknownDraft"])
        self.assertEqual(result["draftAtOpen"], "r")

    def test_open_fails_closed_when_guard_never_confirms(self):
        clock = Clock()
        with self.assertRaisesRegex(RuntimeError, "guard not confirmed; do not send"):
            w.open_tab("u", "w", run=self.created(), guard=lambda _: {"guarded": False, "drafts": [""]}, sleep=clock.sleep, clock=clock, timeout=1)

    def test_wait_and_confirm_rearm_guard_every_observation(self):
        clock, calls = Clock(), []
        with self.assertRaises(TimeoutError):
            w.wait_reply(page(), TASK, timeout=60, read=lambda owned: supported_wait_fixture((lambda _: page())(owned), MESSAGE), sleep=clock.sleep, clock=clock, protect=calls.append, request_proof=fixture_generation(page(), MESSAGE), message=MESSAGE)
        self.assertEqual(calls, ["owned"] * 3)
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "receipt.json"
            receipt.write_text(json.dumps(dict(attempted=True, page="owned", messageSha256=hashlib.sha256(MESSAGE.encode()).hexdigest())))
            calls.clear()
            clock = Clock()
            w.confirm_posting(page(), MESSAGE, receipt, timeout=40, read=lambda _: page(), sleep=clock.sleep, clock=clock, protect=calls.append)
            self.assertEqual(calls, ["owned"] * 3)

    def test_unknown_draft_keeps_guard_on_and_returns_exact_draft(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=page()), patch.object(w, "composer", return_value="r"), \
                 patch.object(w, "guard_input") as guard, patch.object(w, "orca") as browser:
                with self.assertRaises(w.ProtectedDraft) as caught:
                    w.submit(page(), MESSAGE, Path(td) / "receipt.json", protect=lambda _: None, request_proof=fixture_generation(page(), MESSAGE))
                self.assertEqual(caught.exception.draft, "r")
                guard.assert_not_called()
                browser.assert_not_called()

    def test_guard_released_only_after_draft_check_and_before_fill(self):
        ready = {**page(), "refs": {"box": {"role": "textbox", "name":"fixture chat composer"}, "send": {"role": "button", "name": "Send"}}}
        order = []
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=ready), \
                 patch.object(w, "composer", side_effect=lambda _: order.append("composer") or w.ComposerText("" if "fill" not in order else MESSAGE, accessible_name="fixture chat composer")), \
                 patch.object(w, "guard_input", side_effect=lambda p, enabled=True: order.append(("guard", enabled))), \
                 patch.object(w, "orca", side_effect=lambda p, action, *a: order.append(action)), \
                 patch.object(w, "wait_send_ready", return_value=(ready, 'form button[aria-label="Send"]')), \
                 patch.object(w, "confirm_posting", return_value={"posted": True}):
                w.submit(page(), MESSAGE, Path(td) / "receipt.json", protect=lambda _: None, request_proof=fixture_generation(page(), MESSAGE))
        self.assertEqual(order[:4], ["composer", ("guard", False), "fill", "composer"])
        self.assertEqual(order[-1], "click")

    def test_guard_script_blocks_only_trusted_input_while_enabled(self):
        harness = """const listeners={};const window={addEventListener:(t,f,c)=>{if(!c)throw 'capture';(listeners[t]=listeners[t]||[]).push(f)}};
        const blurred=[];const box={innerText:'r',blur:()=>blurred.push(1)};
        const document={activeElement:box,querySelectorAll:()=>[box]};const location={href:'https://chatgpt.com/'};
        const fire=(t,trusted)=>{let blocked=0;const e={isTrusted:trusted,preventDefault:()=>blocked++,stopImmediatePropagation:()=>blocked++};(listeners[t]||[]).forEach(f=>f(e));return blocked};
        const on=JSON.parse(eval(__ON__));const again=JSON.parse(eval(__ON__));
        const out={on,count:listeners.keydown.length,trusted:fire('keydown',true),ime:fire('compositionstart',true),paste:fire('paste',true),tool:fire('input',false),blurred:blurred.length};
        const off=JSON.parse(eval(__OFF__));out.off=off.guarded;out.afterOff=fire('keydown',true);
        process.stdout.write(JSON.stringify(out));"""
        on = w.GUARD_EXPRESSION.replace("__ENABLED__", "true")
        off = w.GUARD_EXPRESSION.replace("__ENABLED__", "false")
        harness = harness.replace("__ON__", json.dumps(on)).replace("__OFF__", json.dumps(off))
        result = json.loads(subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(result["on"], {"guarded": True, "url": "https://chatgpt.com/", "drafts": ["r"]})
        self.assertEqual(result["count"], 1)  # Idempotent across repeated installs.
        self.assertEqual((result["trusted"], result["ime"], result["paste"]), (2, 2, 2))
        self.assertEqual(result["tool"], 0)
        self.assertEqual(result["blurred"], 2)
        self.assertEqual((result["off"], result["afterOff"]), (False, 0))


class ConnectorChipTests(unittest.TestCase):
    """A selected connector is an attachment chip, not a draft or request text."""

    def ready(self):
        return {**page(), "refs": {"box": {"role": "textbox", "name":"fixture chat composer"}, "send": {"role": "button", "name": "Send"}}}

    def run_node(self, harness, expression):
        harness = harness.replace("__EXPR__", json.dumps(expression))
        return json.loads(subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout)

    def fake_dom(self, body_text, chip):
        # Minimal DOM: element whose innerText includes the chip label unless the chip is removed.
        return """const chip=%s;const removed=[];
        const mk=(visible)=>({getAttribute:a=>a==='aria-label'?'Ask ChatGPT':null,get innerText(){return %s+(visible()&&chip?chip:'')},clientWidth:500,
          querySelectorAll:()=>chip&&visible()?[{getAttribute:(a)=>a==='app-mention-display-name'?chip:null,innerText:chip,remove:()=>removed.push(1)}]:[],
          cloneNode:()=>copy});
        const copy=mk(()=>removed.length===0);const e=mk(()=>true);
        const document={body:{appendChild:()=>{}},createElement:()=>({style:{},appendChild:()=>{},remove:()=>{}}),
          querySelectorAll:(s)=>s.includes('textbox')||s.includes('user-message-bubble')?[{querySelector:()=>e,closest:()=>null,value:undefined,...e}]:[]};
        """ % (json.dumps(chip), json.dumps(body_text))

    def test_composer_and_posted_reads_exclude_chip_and_report_it(self):
        dom = self.fake_dom("TASK_ID: t\n", "Code with ChatGPT · alpha-c2c")
        posted = self.run_node(dom + "process.stdout.write(eval(__EXPR__));", w.POSTED_USER_EXPRESSION)
        self.assertEqual(posted["body"], "TASK_ID: t\n")
        self.assertEqual(posted["mentions"], ["Code with ChatGPT · alpha-c2c"])
        plain = self.run_node(self.fake_dom("TASK_ID: t", "") + "process.stdout.write(eval(__EXPR__));", w.POSTED_USER_EXPRESSION)
        self.assertEqual((plain["body"], plain["mentions"]), ("TASK_ID: t", []))

    def test_chip_alone_is_not_a_protected_draft_and_insert_keeps_it(self):
        chip = ["Code with ChatGPT · alpha-c2c"]
        order = []
        reads = iter([w.ComposerText(" ", chip, accessible_name="fixture chat composer"), w.ComposerText(" ", chip, accessible_name="fixture chat composer"), w.ComposerText(" ", chip, accessible_name="fixture chat composer"), w.ComposerText(MESSAGE, chip, accessible_name="fixture chat composer")])
        def browser(p, action, *a):
            order.append(action if action != "eval" else "caret")
            return {"result": "caret-start"} if action == "eval" else {}
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=self.ready()), patch.object(w, "composer", side_effect=lambda _: next(reads)), \
                 patch.object(w, "guard_input"), patch.object(w, "native_focus_editor"), patch.object(w, "orca", side_effect=browser), \
                 patch.object(w, "wait_send_ready", return_value=(self.ready(), 'form button[aria-label="Send"]')), \
                 patch.object(w, "confirm_posting", return_value={"posted": True}):
                self.assertTrue(w.submit(page(), MESSAGE, Path(td) / "receipt.json", protect=lambda _: None, request_proof=fixture_generation(page(), MESSAGE))["posted"])
        self.assertEqual(order, ["caret", "inserttext", "keypress", "inserttext", "keypress", "inserttext", "keypress", "inserttext", "keypress", "inserttext", "keypress", "inserttext", "click"])  # all original lines; no chip-wiping fill

    def test_lost_or_changed_chip_is_never_sent(self):
        chip = ["Code with ChatGPT · alpha-c2c"]
        for after in ([], ["Other connector"]):
            reads = iter([w.ComposerText("", chip, accessible_name="fixture chat composer"), w.ComposerText(MESSAGE, after, accessible_name="fixture chat composer")])
            with self.subTest(after=after), tempfile.TemporaryDirectory() as td:
                with patch.object(w, "read_page", return_value=self.ready()), patch.object(w, "composer", side_effect=lambda _: next(reads)), \
                     patch.object(w, "insert_empty_chip_draft"), patch.object(w, "orca", return_value={"result": "caret-start"}) as browser:
                    with self.assertRaisesRegex(ValueError, "Connector chip changed"):
                        w.submit(page(), MESSAGE, Path(td) / "receipt.json", request_proof=fixture_generation(page(), MESSAGE))
                    self.assertNotIn("click", [c.args[1] for c in browser.call_args_list])

    def test_real_text_beside_chip_stays_protected(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=page()), patch.object(w, "composer", return_value=w.ComposerText("r", ["Code with ChatGPT · alpha-c2c"], accessible_name="fixture chat composer")), \
                 patch.object(w, "orca") as browser:
                with self.assertRaises(w.ProtectedDraft) as caught:
                    w.submit(page(), MESSAGE, Path(td) / "receipt.json", request_proof=fixture_generation(page(), MESSAGE))
                self.assertEqual(caught.exception.draft, "r")
                browser.assert_not_called()

    def test_composer_reader_strips_chip_from_draft_text(self):
        values = [{"text": " ", "mentions": ["Code with ChatGPT · alpha-c2c"]}]
        with patch.object(w, "orca", return_value={"result": json.dumps(values)}) as browser:
            draft = w.composer("owned")
        self.assertEqual((str(draft), draft.mentions), (" ", ["Code with ChatGPT · alpha-c2c"]))
        self.assertIn("withoutMentions", browser.call_args.args[3])
        dom = self.fake_dom("", "Code with ChatGPT · alpha-c2c")
        read = self.run_node(dom + "process.stdout.write(eval(__EXPR__));", w.COMPOSER_EXPRESSION)
        self.assertEqual(read, [{"text": "", "mentions": ["Code with ChatGPT · alpha-c2c"], "accessibleName": "Ask ChatGPT"}])

    def test_caret_failure_never_inserts_or_sends(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=self.ready()), \
                 patch.object(w, "composer", return_value=w.ComposerText("", ["Code with ChatGPT · alpha-c2c"], accessible_name="fixture chat composer")), \
                 patch.object(w, "native_focus_editor"), patch.object(w, "orca", return_value={"result": "no-composer"}) as browser:
                with self.assertRaisesRegex(ValueError, "caret unavailable"):
                    w.submit(page(), MESSAGE, Path(td) / "receipt.json", request_proof=fixture_generation(page(), MESSAGE))
                self.assertEqual([c.args[1] for c in browser.call_args_list], ["eval"])

    def test_posted_request_with_mention_pill_confirms(self):
        current = {**page(MESSAGE, "TASK_ID: task-27\nSTATE: PLAN"), "userBody": MESSAGE + "\n", "userMessageId": "new"}
        self.assertFalse(w.posted(current, {**page(), "userMessageId": "old", "userBody": "old request"}, MESSAGE))


class SendReadinessTests(unittest.TestCase):
    def state(self, **values):
        return {"result": json.dumps(values)}

    def test_disabled_button_waits_then_uses_semantic_selector(self):
        clock = Clock()
        selector = 'form button[aria-label="Send"]'
        with patch.object(w, "composer", return_value=w.ComposerText(MESSAGE, accessible_name="fixture chat composer")), \
             patch.object(w, "orca", side_effect=[self.state(ready=False, reason="send-disabled"), self.state(ready=True, selector=selector)]) as browser:
            current, target = w.wait_send_ready(page(), page(), MESSAGE, [], read=lambda _: page(), clock=clock, sleep=clock.sleep)
        self.assertEqual(target, selector)
        self.assertEqual(clock.now, 0.5)
        self.assertEqual([c.args[1] for c in browser.call_args_list], ["eval", "eval"])

    def test_unready_timeout_does_not_click_or_clear(self):
        clock = Clock()
        with patch.object(w, "composer", return_value=w.ComposerText(MESSAGE, accessible_name="fixture chat composer")), \
             patch.object(w, "orca", return_value=self.state(ready=False, reason="send-obscured")) as browser:
            with self.assertRaisesRegex(ValueError, "draft retained, no click attempted"):
                w.wait_send_ready(page(), page(), MESSAGE, [], timeout=1, read=lambda _: page(), clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.now, 1)
        self.assertTrue(all(c.args[1] in ("eval", "screenshot") for c in browser.call_args_list))
        self.assertEqual(sum(c.args[1] == 'screenshot' for c in browser.call_args_list), 1)

    def test_obscured_layout_capture_requires_another_full_observation(self):
        clock, observations = Clock(), []
        selector = 'form button[aria-label="Send"]'
        def read(_):
            observations.append(clock.now)
            return page()
        with patch.object(w, 'composer', return_value=w.ComposerText(MESSAGE, accessible_name="fixture chat composer")), \
             patch.object(w, 'orca', side_effect=[self.state(ready=False, reason='send-obscured'), {}, self.state(ready=True, selector=selector)]) as browser:
            w.wait_send_ready(page(), page(), MESSAGE, [], read=read, clock=clock, sleep=clock.sleep)
        self.assertEqual(observations, [0, 0.5])
        self.assertEqual([c.args[1] for c in browser.call_args_list], ['eval', 'screenshot', 'eval'])

    def test_prepared_request_is_sent_without_refill_or_chip_loss(self):
        ready = {**page(), 'refs': {'box': {'role': 'textbox', "name":"fixture chat composer"}}}
        for chips in ([], ['Fixture connector']):
            with self.subTest(chips=chips), tempfile.TemporaryDirectory() as td:
                with patch.object(w, 'read_page', return_value=ready), \
                     patch.object(w, 'composer', return_value=w.ComposerText(MESSAGE, chips, accessible_name="fixture chat composer")), \
                     patch.object(w, 'wait_send_ready', return_value=(ready, 'form button[aria-label="Send"]')), \
                     patch.object(w, 'confirm_posting', return_value={'posted':True}), \
                     patch.object(w, 'guard_input') as guard, patch.object(w, 'orca') as browser:
                    w.submit(page(), MESSAGE, Path(td)/'receipt.json', protect=lambda _:None, request_proof=fixture_generation(page(), MESSAGE))
                    self.assertEqual([c.args[1] for c in browser.call_args_list], ['click'])
                    guard.assert_not_called()

    def test_draft_or_chip_change_stops_before_button_probe(self):
        for draft in [w.ComposerText(MESSAGE + "stray", accessible_name="fixture chat composer"), w.ComposerText(MESSAGE, ["other-app"], accessible_name="fixture chat composer")]:
            with self.subTest(draft=str(draft)), patch.object(w, "composer", return_value=draft), patch.object(w, "orca") as browser:
                with self.assertRaisesRegex(ValueError, "Composer or connector changed"):
                    w.wait_send_ready(page(), page(), MESSAGE, [], read=lambda _: page())
                browser.assert_not_called()

    def test_conversation_change_during_ready_wait_stops(self):
        with patch.object(w, "composer") as draft, patch.object(w, "orca") as browser:
            with self.assertRaisesRegex(ValueError, "Conversation identity changed"):
                w.wait_send_ready(page(), page(), MESSAGE, [], read=lambda _: page(url=CHAT + "-other"))
            draft.assert_not_called()
            browser.assert_not_called()

    def test_ready_wait_failure_creates_no_attempt_receipt(self):
        ready = {**page(), "refs": {"box": {"role": "textbox", "name":"fixture chat composer"}}}
        with tempfile.TemporaryDirectory() as td:
            receipt = Path(td) / "receipt.json"
            with patch.object(w, "read_page", return_value=ready), \
                 patch.object(w, "composer", side_effect=[w.ComposerText("", accessible_name="fixture chat composer"), w.ComposerText(MESSAGE, accessible_name="fixture chat composer")]), \
                 patch.object(w, "orca") as browser, \
                 patch.object(w, "wait_send_ready", side_effect=ValueError("Send button not ready")):
                with self.assertRaisesRegex(ValueError, "not ready"):
                    w.submit(page(), MESSAGE, receipt, request_proof=fixture_generation(page(), MESSAGE))
                self.assertFalse(receipt.exists())
                self.assertEqual([c.args[1] for c in browser.call_args_list], ["fill"])

    def test_submit_cli_rearms_guard_on_preclick_failure(self):
        with tempfile.TemporaryDirectory() as td:
            baseline, message, receipt = [Path(td) / name for name in ('baseline.json', 'message.txt', 'receipt.json')]
            baseline.write_text(json.dumps(page()))
            message.write_text(MESSAGE)
            argv = ['wait_reply.py', 'submit', '--baseline', str(baseline), '--message-file', str(message), '--receipt', str(receipt)]
            with patch.object(sys, 'argv', argv), patch.object(w, 'submit', side_effect=ValueError('Send button not ready')), \
                 patch.object(w, 'keep_guarded') as guard, patch('builtins.print'):
                self.assertEqual(w.main(), 2)
                guard.assert_called_once_with('owned')
                self.assertFalse(receipt.exists())

    def test_dom_probe_rejects_disabled_hidden_and_covered_buttons(self):
        harness = """const s=__STATE__;const e={disabled:s.disabled,matches:()=>false,
          getAttribute:n=>n==='aria-label'?'Send':n==='aria-disabled'?s.ariaDisabled:null,
          getBoundingClientRect:()=>({x:1,y:1,width:s.width??10,height:10}),contains:()=>false};
        const form={querySelectorAll:()=>[e]};const box={closest:()=>form};
        const document={querySelectorAll:()=>[box],elementFromPoint:()=>s.covered?{}:e};
        const getComputedStyle=()=>({visibility:'visible',display:'block',pointerEvents:'auto'});
        process.stdout.write(eval(__EXPR__));"""
        for state, reason in [({"disabled":True}, "send-disabled"), ({"ariaDisabled":"true"}, "send-disabled"),
                              ({"width":0}, "send-hidden"), ({"covered":True}, "send-obscured"), ({}, None)]:
            with self.subTest(state=state):
                script = harness.replace('__STATE__', json.dumps(state)).replace('__EXPR__', json.dumps(w.SEND_READY_EXPRESSION))
                result = json.loads(subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True).stdout)
                self.assertEqual(result['ready'], reason is None)
                if reason:
                    self.assertEqual(result['reason'], reason)




class OrderedAuthoredTurnTests(unittest.TestCase):
    """Anonymized posted DOM contracts; no task transcript or browser writes."""
    message = 'TASK_ID: fixture-task WORKSPACE_ID: fixture-space ITERATION: 2 PHASE: REVIEW REQUEST_ID: request-00000000-0000-4000-8000-000000000002 Please review.'
    reply = 'TASK_ID: fixture-task WORKSPACE_ID: fixture-space ITERATION: 2 REQUEST_ID: request-00000000-0000-4000-8000-000000000002 STATE: DONE Completed.'

    def turn(self, role, body, ident, complete=False):
        return dict(role=role, body=body, id=ident, ids=[ident], complete=complete)

    def baseline(self):
        return {**page(), 'postedTurns': [self.turn('user', 'old request', 'before-u'), self.turn('assistant', 'old answer', 'before-a', True)]}

    def current(self):
        return {**page('later unrelated', 'later unrelated answer', users=2, count=2),
                'postedTurns': [self.turn('user', self.message, 'request-u'), self.turn('assistant', self.reply, 'reply-a', True),
                                self.turn('user', 'later unrelated', 'later-u'), self.turn('assistant', 'later unrelated answer', 'later-a', True)]}

    def test_nonlatest_exact_request_and_its_associated_done(self):
        c,b=self.current(),self.baseline()
        self.assertTrue(w.posted(c,b,self.message, request_proof=fixture_generation(b, self.message)))
        self.assertTrue(w.eligible(c,b,'fixture-task',self.message, request_proof=fixture_generation(b, self.message)))
        clock=Clock()
        got=w.wait_reply(b,'fixture-task',message=self.message,read=lambda _:c,clock=clock,sleep=clock.sleep, request_proof=fixture_generation(b, self.message))
        self.assertEqual((got['userMessageId'],got['responseMessageId'],got['text']),('request-u','reply-a',self.reply))
        self.assertEqual(clock.now,20)

    def test_legacy_baseline_cannot_prove_nonlatest_age(self):
        c=self.current();b=page()
        self.assertFalse(w.posted(c,b,self.message))
        self.assertFalse(w.eligible(c,b,'fixture-task',self.message))
        with tempfile.TemporaryDirectory() as td, patch.object(w,'read_page',return_value=c), patch.object(w,'orca') as browser, patch.object(w,'composer') as composer:
            result=w.submit(b,self.message,Path(td)/'receipt.json')
            self.assertIsNone(result['posted']);self.assertTrue(result['alreadyPresent'])
            browser.assert_not_called();composer.assert_not_called()

    def test_same_baseline_exact_request_inhibits_input(self):
        c=self.current()
        with tempfile.TemporaryDirectory() as td, patch.object(w,'read_page',return_value=c), patch.object(w,'orca') as browser:
            result=w.submit(c,self.message,Path(td)/'receipt.json')
            self.assertFalse(result['attempted']);self.assertIsNone(result['posted']);browser.assert_not_called()

    def test_duplicate_body_or_reused_id_is_ambiguous(self):
        for extra in [self.turn('user',self.message,'duplicate-u'),self.turn('assistant','other','request-u',True)]:
            c=self.current();c['postedTurns'].append(extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError,'Ambiguous'):
                w.posted(c,self.baseline(),self.message, request_proof=fixture_generation(self.baseline(), self.message))

    def test_missing_id_cannot_be_new(self):
        c=self.current();c['postedTurns'][0].update(id=None,ids=[])
        self.assertFalse(w.posted(c,self.baseline(),self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_quoted_or_tool_request_is_not_authored_user(self):
        c=self.current();c['postedTurns'][0]['body']='Quoted: '+self.message
        c['postedTurns'].append(self.turn('tool',self.message,'tool-id'))
        self.assertIsNone(w.rendered_request(c,self.message))

    def test_reply_before_request_or_after_next_user_not_associated(self):
        c=self.current();c['postedTurns']=c['postedTurns'][1:2]+c['postedTurns'][0:1]+c['postedTurns'][2:]
        self.assertTrue(w.posted(c,self.baseline(),self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_wrong_missing_conflicting_supplied_identity_rejected(self):
        for reply in [self.reply.replace('ITERATION: 2','ITERATION: 1'),self.reply.replace('WORKSPACE_ID: fixture-space','WORKSPACE_ID: other'),self.reply.replace('WORKSPACE_ID: fixture-space',''),self.reply.replace('ITERATION: 2',''),self.reply.replace('REQUEST_ID: request-00000000-0000-4000-8000-000000000002','REQUEST_ID: other'),self.reply.replace('STATE: DONE','ITERATION: 1 STATE: DONE')]:
            c=self.current();c['postedTurns'][1]['body']=reply
            with self.subTest(reply=reply):self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_completion_elsewhere_or_global_stop_rejected(self):
        c=self.current();c['postedTurns'][1]['complete']=False
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        c=self.current();c['generating']=True
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_full_identity_and_body_must_stabilize(self):
        a=self.current();b=self.current();b['postedTurns'][1]['id']='new-reply';b['postedTurns'][1]['ids']=['new-reply']
        seq=iter([a,b,b]);clock=Clock()
        got=w.wait_reply(self.baseline(),'fixture-task',message=self.message,read=lambda _:next(seq),clock=clock,sleep=clock.sleep, request_proof=fixture_generation(self.baseline(), self.message))
        self.assertEqual(got['responseMessageId'],'new-reply');self.assertEqual(clock.now,40)

    def test_readiness_rechecks_nonlatest_already_posted(self):
        with patch.object(w,'composer') as composer,patch.object(w,'orca') as browser:
            with self.assertRaisesRegex(ValueError,'Posted request appeared'):
                w.wait_send_ready(self.baseline(),self.baseline(),self.message,[],read=lambda _:self.current())
            browser.assert_not_called();composer.assert_not_called()

    def test_fresh_preclick_request_race_creates_no_attempt(self):
        b=self.baseline();b['refs']={'box':{'role':'textbox', "name":"fixture chat composer"}}
        ready={'result':json.dumps({'ready':True,'selector':'form button[aria-label="Send"]'})}
        with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',side_effect=[b,b,self.current()]),patch.object(w,'composer',return_value=w.ComposerText(self.message,accessible_name='fixture chat composer')),patch.object(w,'orca',return_value=ready) as browser:
            receipt=Path(td)/'receipt.json'
            with self.assertRaisesRegex(ValueError,'Posted request appeared'):w.submit(b,self.message,receipt, request_proof=fixture_generation(b, self.message))
            self.assertFalse(receipt.exists())
            self.assertEqual([call.args[1] for call in browser.call_args_list],['eval'])

    def test_confirmed_receipt_survives_virtualized_absence(self):
        with tempfile.TemporaryDirectory() as td,patch.object(w,'orca') as browser:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps({'page':'owned','url':CHAT,'messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'posted':True,'status':'POSTED','userMessageId':'request-u'}))
            r=w.confirm_posting(self.baseline(),self.message,receipt,timeout=0,read=lambda _:self.baseline())
            self.assertTrue(r['posted']);self.assertEqual(r['status'],'POSTED');browser.assert_not_called()

    def test_confirmed_receipt_pins_legacy_nonlatest_request(self):
        with tempfile.TemporaryDirectory() as td:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps({'page':'owned','url':CHAT,'messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'posted':True,'userMessageId':'request-u'}))
            r=w.confirm_posting(page(),self.message,receipt,timeout=0,read=lambda _:self.current())
            self.assertTrue(r['posted']);self.assertEqual(r['userMessageId'],'request-u')

    def test_uncertain_receipt_never_fills_or_clicks_nonlatest_match(self):
        with tempfile.TemporaryDirectory() as td,patch.object(w,'orca') as browser,patch.object(w,'read_page',return_value=self.current()),patch.object(w,'composer') as composer:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps({'page':'owned','messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'attempted':True}))
            # confirm_posting's default read is bound at definition; use actual callable explicitly.
            r=w.confirm_posting(self.baseline(),self.message,receipt,timeout=0,read=lambda _:self.current())
            self.assertIsNone(r['posted']);browser.assert_not_called();composer.assert_not_called()

    def test_actual_dom_shape_extractor_deduplicates_single_line_ids(self):
        # Search units, Korean role headings and shared completion ancestors mirror
        # the supported DOM shape; bodies and IDs are synthetic.
        data=[self.turn('user',self.message,'request-u'),self.turn('assistant',self.reply,'reply-a',True),self.turn('user','later unrelated','later-u')]
        harness=r'''const data=DATA;
        const mkContent=d=>{let tool=true;const c={isConnected:true,get innerText(){return d.body+(d.role==='assistant'&&tool?' TOOL QUOTATION':'')},querySelectorAll:s=>s.includes('data-tool-call')&&d.role==='assistant'?[{remove:()=>tool=false}]:[],cloneNode:()=>mkContent(d)};return c};
        const roots=data.map((d,i)=>({getAttribute:a=>a==='data-chatgpt-search-unit-key'?'fallback-turn-'+i+':'+(d.role==='user'?'0:user':'2:assistant'):a==='data-chatgpt-search-message-ids'?d.id+(d.role==='assistant'?' '+d.id:''):null,
          closest:s=>s==='[data-talvt-turn-state]'?{getAttribute:()=>d.complete?'complete':'pending'}:null,
          querySelector:s=>mkContent(d),querySelectorAll:()=>[]}));
        const bubbles=data.filter(d=>d.role==='user').map(d=>({querySelector:()=>mkContent(d),closest:()=>({getAttribute:()=>d.id})}));
        const document={querySelectorAll:s=>s==='[data-user-message-bubble]'?bubbles:s.includes('data-chatgpt-search-unit-key')?roots:[]};
        process.stdout.write(eval(EXPR));'''.replace('DATA',json.dumps(data)).replace('EXPR',json.dumps(w.POSTED_TURNS_EXPRESSION))
        result=json.loads(subprocess.run(['node','-e',harness],capture_output=True,text=True,check=True).stdout)
        self.assertEqual(result['body'],'later unrelated')
        self.assertEqual(result['turns'][1]['ids'],['reply-a'])
        self.assertTrue(result['turns'][1]['complete'])
        self.assertEqual(result['turns'][1]['body'],self.reply)
        c={**self.current(),'postedTurns':result['turns']}
        self.assertTrue(w.posted(c,self.baseline(),self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        self.assertTrue(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_missing_or_composite_assistant_id_rejected(self):
        for ids in ([],['reply-one','reply-two']):
            c=self.current();c['postedTurns'][1].update(id=None,ids=ids)
            with self.subTest(ids=ids):self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_header_current_identity_ignores_body_example_but_quote_only_rejected(self):
        c=self.current();c['postedTurns'][1]['body']=self.reply+'\nExample: TASK_ID: other ITERATION: 1 STATE: DONE'
        self.assertTrue(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        c['postedTurns'][1]['body']='Example: '+self.reply
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_standalone_c2c_label_between_identity_and_state(self):
        reply=self.reply.replace(' STATE: DONE', '\n\n[C2C]\n\nSTATE: DONE')
        c=self.current();c['postedTurns'][1]['body']=reply
        proof=fixture_generation(self.baseline(),self.message)
        self.assertTrue(w.eligible(c,self.baseline(),'fixture-task',self.message,request_proof=proof))
        clock=Clock()
        got=w.wait_reply(self.baseline(),'fixture-task',message=self.message,
                        read=lambda _:c,clock=clock,sleep=clock.sleep,request_proof=proof)
        self.assertEqual(got['responseMessageId'],'reply-a')
        self.assertEqual(clock.now,20)

    def test_c2c_label_does_not_admit_body_states_or_conflicting_identity(self):
        variants=[
            self.reply.replace(' STATE: DONE', '\n[C2C]\nExample prose\nSTATE: DONE'),
            self.reply.replace(' STATE: DONE', '\n[C2C]\n[C2C]\nSTATE: DONE'),
            self.reply.replace(' STATE: DONE', '\n[C2C] STATE: DONE'),
            '[C2C]\n'+self.reply,
            self.reply.replace(' STATE: DONE', '\n[C2C]\nSTATE: DONE').replace('ITERATION: 2','ITERATION: 1'),
            self.reply.replace(' STATE: DONE', '\n[C2C]\nSTATE: DONE').replace('WORKSPACE_ID: fixture-space',''),
        ]
        for reply in variants:
            with self.subTest(reply=reply):
                c=self.current();c['postedTurns'][1]['body']=reply
                self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message,
                                          request_proof=fixture_generation(self.baseline(),self.message)))

    def test_final_request_id_is_required_in_strict_protocol(self):
        c=self.current();c['postedTurns'][1]['body']=self.reply.replace('REQUEST_ID: request-00000000-0000-4000-8000-000000000002','')
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_confirmed_receipt_wait_pins_old_baseline_without_sending(self):
        with tempfile.TemporaryDirectory() as td,patch.object(w,'orca') as browser:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps(correlated_receipt({'page': 'owned', 'url': CHAT, 'messageSha256': hashlib.sha256(self.message.encode()).hexdigest(), 'posted': True, 'userMessageId': 'request-u'}, page(), self.message)))
            clock=Clock();got=w.wait_reply(page(),'fixture-task',message=self.message,receipt=receipt,read=lambda _:self.current(),clock=clock,sleep=clock.sleep, request_proof=fixture_generation(page(), self.message))
            self.assertEqual(got['responseMessageId'],'reply-a');self.assertEqual(clock.now,20);browser.assert_not_called()
            with self.assertRaisesRegex(ValueError,'different page or request'):
                w.wait_reply(page(),'fixture-task',message=self.message+' changed',receipt=receipt, request_proof=fixture_generation(page(), self.message + ' changed'))

    def test_uncertain_receipt_cannot_pin_stale_reply(self):
        with tempfile.TemporaryDirectory() as td:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps({'page':'owned','messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'posted':None,'userMessageId':'request-u'}))
            clock=Clock()
            with self.assertRaisesRegex(ValueError,"Reply admission HOLD"):w.wait_reply(page(),'fixture-task',message=self.message,receipt=receipt,timeout=40,read=lambda _:self.current(),clock=clock,sleep=clock.sleep)

    def test_prelude_and_final_protocol_segment_same_identity(self):
        c=self.current();c['postedTurns'][1].update(body='Review prelude.\n'+self.reply,protocolSegments=['Review prelude.',self.reply])
        self.assertTrue(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        clock=Clock();got=w.wait_reply(self.baseline(),'fixture-task',message=self.message,read=lambda _:c,clock=clock,sleep=clock.sleep, request_proof=fixture_generation(self.baseline(), self.message))
        self.assertIn('Review prelude.',got['text']);self.assertEqual(clock.now,20)
        c['postedTurns'][1]['protocolSegments']=[self.reply,self.reply.replace('ITERATION: 2','ITERATION: 1')]
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_quoted_code_segments_cannot_supply_protocol(self):
        c=self.current();c['postedTurns'][1].update(body=self.reply,protocolSegments=['','Example prose'])
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))

    def test_confirmed_receipt_identity_drift_does_not_repin(self):
        with tempfile.TemporaryDirectory() as td:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps({'page':'owned','url':CHAT,'messageSha256':hashlib.sha256(self.message.encode()).hexdigest(),'posted':True,'userMessageId':'original-pinned-id'}))
            r=w.confirm_posting(self.baseline(),self.message,receipt,timeout=0,read=lambda _:self.current())
            self.assertTrue(r['posted']);self.assertEqual(r['userMessageId'],'original-pinned-id');self.assertEqual(r['lastObservation'],'REQUEST_ID_DRIFT')

    def test_positive_pin_cannot_be_overridden_by_new_exact_body(self):
        with tempfile.TemporaryDirectory() as td:
            receipt=Path(td)/'receipt.json';receipt.write_text(json.dumps(correlated_receipt({'page': 'owned', 'url': CHAT, 'messageSha256': hashlib.sha256(self.message.encode()).hexdigest(), 'posted': True, 'userMessageId': 'original-pinned-id'}, self.baseline(), self.message)))
            clock=Clock()
            with self.assertRaises(TimeoutError):w.wait_reply(self.baseline(),'fixture-task',message=self.message,receipt=receipt,timeout=40,read=lambda _:self.current(),clock=clock,sleep=clock.sleep, request_proof=fixture_generation(self.baseline(), self.message))
            self.assertEqual(json.loads(receipt.read_text())['userMessageId'],'original-pinned-id')

    def test_fresh_preclick_chip_drift_creates_no_attempt(self):
        b=self.baseline();b['refs']={'box':{'role':'textbox', "name":"fixture chat composer"}}
        ready={'result':json.dumps({'ready':True,'selector':'form button[aria-label="Send"]'})}
        drafts=[w.ComposerText(self.message,[], accessible_name="fixture chat composer") for _ in range(3)]+[w.ComposerText(self.message,['changed'], accessible_name="fixture chat composer")]
        with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=b),patch.object(w,'composer',side_effect=drafts),patch.object(w,'orca',return_value=ready) as browser:
            receipt=Path(td)/'receipt.json'
            with self.assertRaisesRegex(ValueError,'Composer or connector changed'):w.submit(b,self.message,receipt, request_proof=fixture_generation(b, self.message))
            self.assertFalse(receipt.exists());self.assertEqual([call.args[1] for call in browser.call_args_list],['eval'])


    def test_missing_ax_headings_still_scans_authored_dom_and_never_duplicates(self):
        turns=self.current()['postedTurns']
        snapshot={'browserPageId':'owned','origin':CHAT,'snapshot':'- textbox "Composer" [ref=box]','refs':{'box':{'role':'textbox', "name":"fixture chat composer"}}}
        dom={'origin':CHAT,'result':json.dumps({'body':'later unrelated','id':'later-u','turns':turns})}
        with patch.object(w,'orca',side_effect=[snapshot,dom]) as browser:
            c=w.read_page('owned')
            self.assertEqual(len(browser.call_args_list),2)
        self.assertEqual(c['postedTurns'],turns)
        with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=c),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
            w.submit(c,self.message,Path(td)/'receipt.json', request_proof=fixture_generation(c, self.message))
            browser.assert_not_called();composer.assert_not_called()

    def test_empty_dom_roots_exact_ax_request_inhibits_send_but_cannot_confirm(self):
        c={**page(self.message,self.reply),'postedTurns':[]}
        self.assertIsNotNone(w.rendered_request(c,self.message))
        self.assertFalse(w.posted(c,self.baseline(),self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        self.assertFalse(w.eligible(c,self.baseline(),'fixture-task',self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=c),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
            result=w.submit(c,self.message,Path(td)/'receipt.json', request_proof=fixture_generation(c, self.message))
            self.assertIsNone(result['posted']);self.assertFalse(result['attempted'])
            browser.assert_not_called();composer.assert_not_called()

    def test_empty_dom_roots_latest_dom_exact_request_inhibits_send(self):
        c={**page('AX omits inert text','pending'),'userBody':self.message,'userMessageId':'request-u','postedTurns':[]}
        self.assertFalse(w.posted(c,self.baseline(),self.message, request_proof=fixture_generation(self.baseline(), self.message)))
        with tempfile.TemporaryDirectory() as td,patch.object(w,'read_page',return_value=c),patch.object(w,'orca') as browser:
            result=w.submit(c,self.message,Path(td)/'receipt.json', request_proof=fixture_generation(c, self.message))
            self.assertIsNone(result['posted']);browser.assert_not_called()

    def test_genuine_transient_blank_does_not_attempt_dom_eval(self):
        snapshot={'browserPageId':'owned','origin':'about:blank','snapshot':'(empty page)','refs':{}}
        with patch.object(w,'orca',return_value=snapshot) as browser:
            current=w.read_page('owned')
            self.assertTrue(current['emptyDocument']);self.assertEqual(browser.call_count,1)


    def test_markdownroot_class_tokens_preserve_all_authored_segments(self):
        harness=r"""const make=(body,cls)=>({classList:[cls],matches:()=>false,contains:()=>false,closest:()=>null,
          cloneNode:()=>{let tool=true,quote=true;return {isConnected:true,get innerText(){return body+(tool?' TOOL PAYLOAD':'')+(quote?' QUOTED CODE':'')},querySelectorAll:s=>s.includes('data-tool-call')?[{remove:()=>tool=false}]:s.includes('blockquote')?[{remove:()=>quote=false}]:[],cloneNode:()=>make(body,cls).cloneNode()}}});
        const segments=[make('Review prelude.','MarkdownRoot-fixture-a'),make(REPLY,'MarkdownRoot-fixture-b')];
        const root=(role,id)=>({getAttribute:a=>a==='data-chatgpt-search-unit-key'?'fallback-turn-0:'+(role==='user'?'0:user':'2:assistant'):a==='data-chatgpt-search-message-ids'?id+' '+id:null,
          closest:s=>s==='[data-talvt-turn-state]'?{getAttribute:()=> 'complete'}:null,
          querySelector:s=>role==='user'?{isConnected:true,innerText:MESSAGE,querySelectorAll:()=>[],cloneNode(){return this}}:null,
          querySelectorAll:s=>role==='assistant'&&s==='.markdown,[class]'?segments:[]});
        const document={querySelectorAll:s=>s.includes('data-chatgpt-search-unit-key')?[root('user','u'),root('assistant','a')]:[]};
        process.stdout.write(eval(EXPR));""".replace('REPLY',json.dumps(self.reply)).replace('MESSAGE',json.dumps(self.message)).replace('EXPR',json.dumps(w.POSTED_TURNS_EXPRESSION))
        result=json.loads(subprocess.run(['node','-e',harness],capture_output=True,text=True,check=True).stdout)
        self.assertEqual([t['role'] for t in result['turns']],['user','assistant'])
        assistant=result['turns'][1]
        self.assertEqual(assistant['ids'],['a']);self.assertTrue(assistant['complete'])
        self.assertIn('Review prelude.',assistant['body']);self.assertIn(self.reply,assistant['body'])
        self.assertNotIn('TOOL PAYLOAD',assistant['body']);self.assertIn('QUOTED CODE',assistant['body'])
        self.assertEqual(assistant['protocolSegments'],['Review prelude.',self.reply])
        current={**self.current(),'postedTurns':result['turns']}
        self.assertTrue(w.eligible(current,self.baseline(),'fixture-task',self.message,request_proof=fixture_generation(self.baseline(),self.message)))


    def test_detached_no_chip_clone_uses_rendered_host_and_cleans_up(self):
        expected='TASK_ID: fixture-task\n\nWORKSPACE_ID: fixture-space\nITERATION: 2\n\nREQUEST_ID: fixture-request\n\nSTATE: DONE\n\nColumn\tValue'
        harness=r"""let attached=0,removed=0;const make=()=>({isConnected:false,clientWidth:0,
          querySelectorAll:()=>[],cloneNode:()=>make(),get innerText(){return this.isConnected?TEXT:TEXT.replace(/\n|\t/g,'')}});
        const e=make();const document={body:{appendChild:h=>{attached++;h.child.isConnected=true}},createElement:()=>({style:{},appendChild(c){this.child=c},remove(){removed++;this.child.isConnected=false}})};
        const read=eval(EXPR+';withoutMentions(e)');process.stdout.write(JSON.stringify({read,attached,removed,sourceConnected:e.isConnected}));""".replace('TEXT',json.dumps(expected)).replace('EXPR',json.dumps(w.WITHOUT_MENTIONS))
        got=json.loads(subprocess.run(['node','-e',harness],capture_output=True,text=True,check=True).stdout)
        self.assertEqual(got['read'],{'text':expected,'mentions':[]})
        self.assertEqual((got['attached'],got['removed'],got['sourceConnected']),(1,1,False))


    def test_valid_preparation_survives_already_present_unsupported_dom_receipt(self):
        producer_spec=importlib.util.spec_from_file_location('present_producer',Path(w.__file__).with_name('c2c.py'))
        producer=importlib.util.module_from_spec(producer_spec);producer_spec.loader.exec_module(producer)
        baseline=self.baseline()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);capture=root/'baseline.json';capture.write_text(json.dumps(baseline))
            output=root/'request.txt';proof_path=root/'proof.json';receipt=root/'receipt.json'
            producer.prepare_request('review','fixture-task','fixture-space','Korean',2,'Synthetic evidence.',capture,output,proof_path)
            message=output.read_text();proof=json.loads(proof_path.read_text())
            unsupported={**page(message,''),'userBody':message,'postedTurns':[]}
            with patch.object(w,'read_page',return_value=unsupported),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
                result=w.submit(baseline,message,receipt,request_proof=proof_path)
                self.assertIsNone(result['posted']);self.assertFalse(result['attempted'])
                browser.assert_not_called();composer.assert_not_called()
            archived=json.loads(receipt.read_text())
            self.assertEqual(archived.get('generationProof'),proof)
            self.assertEqual(archived.get('generationProofSha256'),w.canonical_sha(proof))
            reply=' '.join(k+': '+v for k,v in proof['identity'].items())+' STATE: DONE'
            restored={**unsupported,'postedTurns':[self.turn('user',message,'restored-u'),self.turn('assistant',reply,'restored-a',True)]}
            with patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
                confirmed=w.confirm_posting(baseline,message,receipt,timeout=0,read=lambda _:restored)
                self.assertTrue(confirmed['posted']);self.assertEqual(confirmed['userMessageId'],'restored-u')
                clock=Clock();got=w.wait_reply(baseline,'fixture-task',message=message,receipt=receipt,read=lambda _:restored,clock=clock,sleep=clock.sleep)
                self.assertEqual(got['responseMessageId'],'restored-a');self.assertEqual(clock.now,20)
                browser.assert_not_called();composer.assert_not_called()
            legacy=root/'legacy.json'
            with patch.object(w,'read_page',return_value=unsupported),patch.object(w,'orca') as browser,patch.object(w,'composer') as composer:
                no_proof=w.submit(baseline,message,legacy)
                self.assertIsNone(no_proof['posted']);self.assertNotIn('generationProof',json.loads(legacy.read_text()))
                before=legacy.read_bytes()
                with self.assertRaisesRegex(ValueError,'retrospective proof graft'):w.submit(baseline,message,legacy,request_proof=proof_path)
                self.assertEqual(legacy.read_bytes(),before);browser.assert_not_called();composer.assert_not_called()


class PostedInlineCodeTests(unittest.TestCase):
    def turn(self, body=None, marked=None, **overrides):
        request = MESSAGE + '\nInspect `<user_query>` exactly.'
        return dict(role='user', id='fresh-inline-u', ids=['fresh-inline-u'],
                    body=request.replace('`', '') if body is None else body,
                    inlineCodeBodyVersion='user-inline-code-single-backtick.v1', inlineCodeMarkCount=1,
                    inlineCodeMarkedBody=request if marked is None else marked, **overrides)

    def test_actual_inline_dom_markers_qualify_without_stripping_request(self):
        request = MESSAGE + '\nInspect `<user_query>` exactly.'
        selected = w.rendered_request(dict(text="", postedTurns=[self.turn()]), request)
        self.assertEqual(selected['userBody'], request)
        self.assertEqual(selected['visibleUserBody'], request.replace('`', ''))
        self.assertEqual(selected['postedBodyMode'], 'dom-single-backtick-inline-code')

    def test_plain_backtick_loss_without_dom_evidence_stays_unconfirmed(self):
        request = MESSAGE + '\nInspect `<user_query>` exactly.'
        turn = self.turn()
        for key in ('inlineCodeBodyVersion','inlineCodeMarkCount','inlineCodeMarkedBody'):
            turn.pop(key)
        self.assertIsNone(w.rendered_request(dict(text="", postedTurns=[turn]),request))

    def test_altered_code_or_wrong_marker_count_never_matches(self):
        request = MESSAGE + '\nInspect `<user_query>` exactly.'
        for marked in (request.replace('user_query','other_query'),request+' changed',request.replace('`','``')):
            self.assertIsNone(w.rendered_request(dict(text="", postedTurns=[self.turn(marked=marked)]),request))
        for count in (0,65,True):
            turn=self.turn();turn['inlineCodeMarkCount']=count
            self.assertIsNone(w.rendered_request(dict(text="", postedTurns=[turn]),request))

    def test_two_qualified_bodies_are_ambiguous_and_never_send(self):
        request = MESSAGE + '\nInspect `<user_query>` exactly.'
        first=self.turn();second={**self.turn(),'id':'different-u','ids':['different-u']}
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            w.rendered_request(dict(text="", postedTurns=[first,second]),request)

    def test_qualified_existing_post_inhibits_input_and_send(self):
        request = MESSAGE + '\nInspect `<user_query>` exactly.'
        current={**page(), 'postedTurns':[self.turn()]}
        with tempfile.TemporaryDirectory() as td, patch.object(w,'read_page',return_value=current), patch.object(w,'composer') as composer,patch.object(w,'orca') as browser:
            result=w.submit(current,request,Path(td)/'receipt.json',request_proof=fixture_generation(current,request))
            self.assertFalse(result['attempted']);composer.assert_not_called();browser.assert_not_called()

    def test_real_js_requires_simple_inline_node_and_keeps_visible_body(self):
        start=w.POSTED_TURNS_EXPRESSION.index("const marked=")
        end=w.POSTED_TURNS_EXPRESSION.index("copy.querySelectorAll('blockquote",start)
        fragment=w.POSTED_TURNS_EXPRESSION[start:end]
        harness=r'''const role='user';const make=()=>{let value=VALUE;
          const code={textContent:VALUE,children:CHILDREN,closest:()=>PRE,
            replaceWith:n=>value=n.textContent};
          return {cloneNode:()=>make(),querySelectorAll:()=>[code],get innerText(){return 'prefix '+value+' suffix'}}};
          const copy=make(),document={createTextNode:textContent=>({textContent})};
          const withoutMentions=e=>({text:e.innerText,mentions:[]});
          const got=(()=>{FRAGMENT;return authored})();
          process.stdout.write(JSON.stringify({got,visible:copy.innerText}));'''
        for value,pre,children,qualifies in [('<user_query>',False,[],True),('has`tick',False,[],False),('line\ncode',False,[],False),('block',True,[],False),('nested',False,[{}],False)]:
            script=harness.replace('VALUE',json.dumps(value)).replace('CHILDREN',json.dumps(children)).replace('PRE',json.dumps(pre)).replace('FRAGMENT',fragment)
            result=json.loads(subprocess.run(['node','-e',script],capture_output=True,text=True,check=True).stdout)
            self.assertEqual(bool(result['got']),qualifies)
            self.assertEqual(result['visible'],'prefix '+value+' suffix')
            if qualifies:self.assertEqual(result['got']['inlineCodeMarkedBody'],'prefix `'+value+'` suffix')


if __name__ == "__main__":
    unittest.main()
