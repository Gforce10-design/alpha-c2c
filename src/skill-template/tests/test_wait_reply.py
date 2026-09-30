"""Delivery regressions; fake snapshots and clocks, never browser writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("waiter", Path(__file__).resolve().parents[1] / "scripts/wait_reply.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
TASK = "task-27"
MESSAGE = "TASK_ID: task-27\nITERATION: 27\nSTATE: PLAN_REQUEST\nPlease plan."
PROJECT = "https://chatgpt.com/g/g-p-abc/project"
CHAT = "https://chatgpt.com/g/g-p-abc-name/c/chat1"


def text(value):
    return "- StaticText " + json.dumps(value)


def page(user="old request", reply="old answer", url=CHAT, users=5, count=5, complete=True, generating=False):
    return dict(page="owned", url=url, userCount=users, count=count, lastUser=text(user) if user else "",
                text=text(reply) if reply else "", complete=complete, generating=generating, refs={})


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
        self.assertTrue(w.posted(self.posted, self.baseline, MESSAGE))

    def test_decreasing_counts_still_match_changed_exact_user_body(self):
        current = {**self.posted, "userCount": 3, "count": 3}
        self.assertTrue(w.posted(current, self.baseline, MESSAGE))
        self.assertTrue(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_quoted_request_or_composer_is_not_posting(self):
        current = page("Someone quoted: " + MESSAGE)
        self.assertFalse(w.posted(current, self.baseline, MESSAGE))
        self.assertFalse(w.posted(self.baseline, self.baseline, MESSAGE))

    def test_same_previous_request_and_fixed_counts_is_ambiguous(self):
        self.assertFalse(w.posted(self.posted, self.posted, MESSAGE))

    def test_markdown_split_state_nodes_and_fixed_counts_complete(self):
        current = {**self.posted, "text": text("TASK_ID: task-27") + "\n" + text("STATE:") + "\n" + text("DONE")}
        self.assertTrue(w.eligible(current, self.baseline, TASK, MESSAGE))

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
            self.assertTrue(result["posted"])
            self.assertEqual(clock.now, 80)
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
                self.assertTrue(result["posted"])
                self.assertEqual(json.loads(receipt.read_text())["status"], "POSTED")

    def test_wait_survives_unchanged_counts_before_delayed_reply(self):
        clock = Clock()
        reads = []
        def read(_):
            reads.append(clock.now)
            return self.baseline if clock.now < 80 else self.posted
        result = w.wait_reply(self.baseline, TASK, timeout=140, read=read, sleep=clock.sleep, clock=clock, message=MESSAGE)
        self.assertEqual(result, self.posted)
        self.assertEqual(reads[-2:], [80, 100])

    def test_wait_timeout_never_asserts_not_posted(self):
        clock = Clock()
        with self.assertRaisesRegex(TimeoutError, "unconfirmed/pending"):
            w.wait_reply(self.baseline, TASK, timeout=40, read=lambda _: self.baseline, sleep=clock.sleep, clock=clock)

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
            result = w.wait_reply(self.baseline, TASK, timeout=200, read=lambda _: next(seq),
                                  sleep=clock.sleep, clock=clock, message=MESSAGE)
            browser.assert_not_called()
        self.assertEqual(result, self.posted)
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
                self.assertTrue(result['posted'])
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
            w.wait_reply(self.baseline, TASK, timeout=80, read=lambda _: blank,
                         sleep=clock.sleep, clock=clock, message=MESSAGE)

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
        w.check_identity({**blank,'url':PROJECT,'refs':{'box':{'role':'textbox'}}},page(url=PROJECT))

    def test_acquired_new_conversation_is_pinned_during_wait(self):
        baseline = page("", "", PROJECT, users=0, count=0)
        seq = iter([self.posted, {**self.posted, "url": CHAT.replace("chat1", "chat2")}])
        clock = Clock()
        with self.assertRaisesRegex(ValueError, "Conversation identity changed"):
            w.wait_reply(baseline, TASK, read=lambda _: next(seq), sleep=clock.sleep, clock=clock, message=MESSAGE)

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
                    w.submit(self.baseline, MESSAGE, Path(td) / "receipt.json")
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
        result = w.wait_reply(self.baseline, TASK, read=lambda _: next(seq), clock=clock, sleep=clock.sleep, message=MESSAGE)
        self.assertIn("updated", result["text"])
        self.assertEqual(clock.now, 40)

    def test_task_id_prefix_is_not_identity(self):
        current = {**self.posted, "text": text("TASK_ID: task-270 STATE: DONE")}
        self.assertFalse(w.eligible(current, self.baseline, TASK, MESSAGE))

    def test_dom_body_restores_inert_links_and_excludes_controls(self):
        message = MESSAGE + "\nReference: https://example.com/design"
        current = {**self.posted, "userBody": message,
                   "lastUser": text(MESSAGE + "\nReference: ") + '\n- button "More"\n' + text("More")}
        self.assertTrue(w.posted(current, self.baseline, message))
        self.assertFalse(w.posted({**current, "userBody": None}, self.baseline, message))

    def test_toolbar_changes_are_not_new_user_messages(self):
        current = {**self.baseline, "lastUser": self.baseline["lastUser"] + '\n- button "More"\n' + text("More")}
        self.assertFalse(w.new_user_turn(current, self.baseline))

    def test_dom_user_identity_disambiguates_identical_virtualized_requests(self):
        before = {**self.posted, "userBody": MESSAGE, "userMessageId": "turn1"}
        current = {**before, "userMessageId": "turn2"}
        self.assertTrue(w.posted(current, before, MESSAGE))

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
        harness = """const old={innerText:'ITERATION: 27'};const latest={innerText:'ITERATION: 28'};
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
        result = w.wait_reply(self.baseline, TASK, read=lambda _: next(seq), clock=clock, sleep=clock.sleep, message=MESSAGE)
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
            w.wait_reply(page(), TASK, timeout=60, read=lambda _: page(), sleep=clock.sleep, clock=clock, protect=calls.append)
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
                    w.submit(page(), MESSAGE, Path(td) / "receipt.json", protect=lambda _: None)
                self.assertEqual(caught.exception.draft, "r")
                guard.assert_not_called()
                browser.assert_not_called()

    def test_guard_released_only_after_draft_check_and_before_fill(self):
        ready = {**page(), "refs": {"box": {"role": "textbox"}, "send": {"role": "button", "name": "Send"}}}
        order = []
        with tempfile.TemporaryDirectory() as td:
            with patch.object(w, "read_page", return_value=ready), \
                 patch.object(w, "composer", side_effect=lambda _: order.append("composer") or ("" if "fill" not in order else MESSAGE)), \
                 patch.object(w, "guard_input", side_effect=lambda p, enabled=True: order.append(("guard", enabled))), \
                 patch.object(w, "orca", side_effect=lambda p, action, *a: order.append(action)), \
                 patch.object(w, "confirm_posting", return_value={"posted": True}):
                w.submit(page(), MESSAGE, Path(td) / "receipt.json", protect=lambda _: None)
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


if __name__ == "__main__":
    unittest.main()
