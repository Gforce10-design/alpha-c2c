"""Real browser expressions against several visible editors; no browser writes."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_wait_reply import w, page, MESSAGE, fixture_generation


class FormComposerTests(unittest.TestCase):
    def browser_expression(self, count=1):
        expression = w.COMPOSER_EXPRESSION.replace(
            w.WITHOUT_MENTIONS,
            "const withoutMentions=e=>{if(e.kind==='code')throw Error('Read unrelated code editor');return {text:e.text,mentions:[]}};",
        )
        harness = """
const code={kind:'code',getAttribute:()=> 'Code editing'};
const chat={kind:'chat',text:'OWNED REQUEST',getAttribute:()=> 'Ask ChatGPT'};
const forms=Array(COUNT).fill(chat),all=[code,code,...forms];
const document={querySelectorAll:selector=>selector.includes('form ')?forms:all};
process.stdout.write(eval(EXPR));
""".replace("COUNT", str(count)).replace("EXPR", json.dumps(expression))
        return subprocess.run(["node", "-e", harness], capture_output=True, text=True)

    def test_actual_expression_does_not_read_unrelated_code_editors(self):
        result = self.browser_expression()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["text"], "OWNED REQUEST")
        self.assertEqual(values[0]["accessibleName"], "Ask ChatGPT")

    def test_two_chat_form_editors_still_refuse(self):
        result = self.browser_expression(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(w, "orca", return_value={"result": result.stdout}):
            with self.assertRaisesRegex(ValueError, "Expected one composer"):
                w.composer("owned")

    def test_absent_chat_form_editor_still_refuses(self):
        result = self.browser_expression(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(w, "orca", return_value={"result": result.stdout}):
            with self.assertRaisesRegex(ValueError, "Expected one composer"):
                w.composer("owned")

    def current(self, duplicate=False):
        refs = {"code1": {"role": "textbox", "name": "Code editing"},
                "code2": {"role": "textbox", "name": "Code editing"},
                "chat": {"role": "textbox", "name": "Ask ChatGPT"}}
        if duplicate:
            refs["other"] = {"role": "textbox", "name": "Ask ChatGPT"}
        return {**page(), "refs": refs}

    def draft(self, name="Ask ChatGPT"):
        draft = w.ComposerText(MESSAGE)
        draft.accessible_name = name
        return draft

    def test_submit_binds_fresh_dom_name_to_unique_accessible_chat_editor(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(w, "read_page", return_value=self.current()), \
             patch.object(w, "composer", return_value=self.draft()), \
             patch.object(w, "wait_send_ready", return_value=(self.current(), 'form button[aria-label="Send"]')), \
             patch.object(w, "confirm_posting", return_value={"posted": True}), \
             patch.object(w, "orca") as browser:
            result = w.submit(page(), MESSAGE, Path(td)/"receipt.json", request_proof=fixture_generation(page(), MESSAGE))
            self.assertTrue(result["posted"])
            self.assertEqual([call.args[1] for call in browser.call_args_list], ["click"])

    def test_ambiguous_or_unbound_accessible_editor_names_never_mutate(self):
        for duplicate, name in [(True, "Ask ChatGPT"), (False, None), (False, "Unknown")]:
            with self.subTest(duplicate=duplicate, name=name), tempfile.TemporaryDirectory() as td, \
                 patch.object(w, "read_page", return_value=self.current(duplicate)), \
                 patch.object(w, "composer", return_value=self.draft(name)), \
                 patch.object(w, "orca") as browser:
                receipt = Path(td)/"receipt.json"
                with self.assertRaisesRegex(ValueError, "Expected one accessible composer"):
                    w.submit(page(), MESSAGE, receipt, request_proof=fixture_generation(page(), MESSAGE))
                self.assertFalse(receipt.exists())
                browser.assert_not_called()

    def test_single_unrelated_or_unnamed_ax_editor_never_mutates(self):
        for meta in [{"role": "textbox", "name": "Code editing"}, {"role": "textbox"}]:
            with self.subTest(meta=meta), tempfile.TemporaryDirectory() as td, \
                 patch.object(w, "read_page", return_value={**page(), "refs": {"code": meta}}), \
                 patch.object(w, "composer", return_value=self.draft()), \
                 patch.object(w, "orca") as browser:
                receipt = Path(td)/"receipt.json"
                with self.assertRaisesRegex(ValueError, "Expected one accessible composer"):
                    w.submit(page(), MESSAGE, receipt, request_proof=fixture_generation(page(), MESSAGE))
                self.assertFalse(receipt.exists())
                browser.assert_not_called()

    def test_guard_caret_send_and_focus_use_chat_form_selectors(self):
        for expression in [w.GUARD_EXPRESSION, w.CARET_START_EXPRESSION, w.SEND_READY_EXPRESSION]:
            self.assertIn("form [role=textbox][contenteditable=true]", expression)
        import ast
        tree = ast.parse(Path(w.__file__).read_text())
        for name in ["native_focus_editor", "replace_owned_chip_draft"]:
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
            for node in ast.walk(function):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "querySelectorAll('[role=textbox]" in node.value:
                    self.fail(name+" retains an unbound editor selector")


if __name__ == "__main__":
    unittest.main()
