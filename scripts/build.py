#!/usr/bin/env python3
"""Build two standalone skills from one maintained workflow. No network calls."""
from pathlib import Path
import argparse

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = {
    "codex": {"__SKILL_NAME__": "alpha-c2c-codex", "__EXECUTOR__": "Codex", "__EXECUTOR_ID__": "codex", "__BRIDGE__": "c2c"},
    "claude": {"__SKILL_NAME__": "alpha-c2c-claude", "__EXECUTOR__": "Claude Code", "__EXECUTOR_ID__": "claude", "__BRIDGE__": "code-with-chatgpt"},
}


def build(check=False):
    stale = []
    for client, replacements in VARIANTS.items():
        dest = ROOT / "skills" / replacements["__SKILL_NAME__"]
        expected = set()
        for source in sorted((ROOT / "src/skill-template").rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(ROOT / "src/skill-template")
            if client == "claude" and relative.parts[0] == "agents":
                continue
            target = dest / relative
            expected.add(target)
            content = source.read_text(encoding="utf-8")
            for key, value in replacements.items():
                content = content.replace(key, value)
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                stale.append(str(target.relative_to(ROOT)))
                if not check:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
        extra = [p for p in dest.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p not in expected]
        if extra:
            raise ValueError("Unexpected files in generated skill; inspect rather than deleting: " + ", ".join(str(p) for p in extra))
    return stale


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = build(args.check)
    print("Skill packages are current." if not stale else "Updated files: " + str(len(stale)) if not args.check else "Stale files: " + ", ".join(stale))
    raise SystemExit(1 if args.check and stale else 0)
