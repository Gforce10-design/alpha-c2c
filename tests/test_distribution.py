"""Distribution: installs follow each user's own client settings and carry no personal paths."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

# Absolute user-home paths, machine-specific app profiles, and session scratch paths.
PERSONAL = re.compile(r"/Users/(?!<|\$|\{|example|you\b|name\b)[A-Za-z0-9._-]+|/home/(?!<|\$|\{|user\b|runner\b)[A-Za-z0-9._-]+/"
                      r"|[A-Za-z]:\\\\?Users\\\\?(?!<|%|example)[A-Za-z0-9._-]+|claude-profiles/[0-9a-f]{8}-|/private/tmp/claude-")


class InstallLocationTests(unittest.TestCase):
    def test_client_settings_choose_the_skill_directory(self):
        home = Path("/h")
        self.assertEqual(installer.skill_dirs("claude", {}, home), [home / ".claude/skills"])
        self.assertEqual(installer.skill_dirs("claude", {"CLAUDE_CONFIG_DIR": "/cfg"}, home),
                         [Path("/cfg/skills"), home / ".claude/skills"])
        self.assertEqual(installer.skill_dirs("codex", {}, home), [home / ".agents/skills", home / ".codex/skills"])
        self.assertEqual(installer.skill_dirs("codex", {"CODEX_HOME": "/cx"}, home)[0], Path("/cx/skills"))

    def test_existing_install_location_wins_over_new_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".agents/skills/alpha-c2c-codex").mkdir(parents=True)
            env = {"CODEX_HOME": str(home / "cx")}
            self.assertEqual(installer.default_dir("codex", env, home), home / ".agents/skills")
            self.assertEqual(installer.default_dir("claude", {}, home), home / ".claude/skills")

    def test_detect_clients_from_their_own_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            none = lambda name: None
            self.assertEqual(installer.detect_clients({}, home, none), [])
            (home / ".codex").mkdir()
            self.assertEqual(installer.detect_clients({}, home, none), ["codex"])
            self.assertEqual(installer.detect_clients({"CLAUDE_CONFIG_DIR": "/c"}, home, none), ["claude", "codex"])


class UpdateTests(unittest.TestCase):
    def test_update_backs_up_outside_the_skills_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory) / "skills"
            path, _ = installer.install("claude", skills)
            (path / "SKILL.md").write_text("---\nname: alpha-c2c-claude\n---\nold edits\n")
            now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            _, action = installer.install("claude", skills, update=True, now=now)
            backup = Path(directory) / ".alpha-c2c-backups/alpha-c2c-claude-20260102T030405Z"
            self.assertIn("updated", action)
            self.assertIn("old edits", (backup / "SKILL.md").read_text())
            self.assertEqual(installer.files(ROOT / "skills/alpha-c2c-claude"), installer.files(path))
            self.assertEqual(sorted(p.name for p in skills.iterdir()), ["alpha-c2c-claude"])

    def test_update_refuses_a_directory_that_is_not_this_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory)
            foreign = skills / "alpha-c2c-codex"
            foreign.mkdir()
            (foreign / "SKILL.md").write_text("---\nname: someone-else\n---\n")
            with self.assertRaisesRegex(ValueError, "not an alpha-c2c-codex install"):
                installer.install("codex", skills, update=True)
            self.assertEqual((foreign / "SKILL.md").read_text(), "---\nname: someone-else\n---\n")

    def test_local_additions_live_outside_the_skill_and_survive_update(self):
        for client in ("claude", "codex"):
            self.assertIn("alpha-c2c/local.md", (ROOT / f"skills/alpha-c2c-{client}/SKILL.md").read_text())
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            local = home / ".config/alpha-c2c/local.md"
            local.parent.mkdir(parents=True)
            local.write_text("my private workflow\n")
            skills = home / ".claude/skills"
            path, _ = installer.install("claude", skills)
            (path / "SKILL.md").write_text("---\nname: alpha-c2c-claude\n---\nedited\n")
            installer.install("claude", skills, update=True)
            self.assertEqual(local.read_text(), "my private workflow\n")

    def test_cli_dry_run_reports_targets_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(installer.main(["--client", "codex", "--skills-dir", directory, "--dry-run"]), 0)
            self.assertIn("alpha-c2c-codex", out.getvalue())
            self.assertEqual(list(Path(directory).iterdir()), [])


class PublishedContentTests(unittest.TestCase):
    def tracked(self):
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"],
                                capture_output=True, text=True)
        if listed.returncode:
            return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
        return [ROOT / line for line in listed.stdout.splitlines() if (ROOT / line).is_file()]

    def test_no_personal_or_machine_paths_are_published(self):
        leaks = []
        for path in self.tracked():
            # This detector holds the sample shapes it rejects.
            if path.suffix in (".png", ".jpg", ".ico") or "__pycache__" in path.parts or path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            leaks += ["%s: %s" % (path.relative_to(ROOT), m.group(0)) for m in PERSONAL.finditer(text)]
        self.assertEqual(leaks, [])

    def test_personal_path_pattern_catches_real_shapes(self):
        for sample in ("/Users/alice/x", "/home/bob/x", "C:\\Users\\carol", "orca/claude-profiles/0123abcd-ef"):
            self.assertRegex(sample, PERSONAL)
        for sample in ("/Users/<you>/x", "$HOME/.claude", "~/.agents/skills"):
            self.assertNotRegex(sample, PERSONAL)

    def test_plugin_manifests_point_at_the_generated_claude_skill(self):
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        source = market["plugins"][0]["source"]
        plugin = json.loads((ROOT / source / ".claude-plugin/plugin.json").read_text())
        self.assertEqual((source, plugin["name"], plugin["skills"]), ("./skills/alpha-c2c-claude", "alpha-c2c", "./"))
        self.assertFalse((ROOT / "skills/alpha-c2c-codex/.claude-plugin").exists())


if __name__ == "__main__":
    unittest.main()
