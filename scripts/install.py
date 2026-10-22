#!/usr/bin/env python3
"""Install a standalone skill without changing bridge state or account settings."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import os
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ("codex", "claude")


def files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def skill_dirs(client, env=None, home=None):
    """The client's user skill directories, preferred first, from its own settings."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if client == "claude":
        found = [Path(env["CLAUDE_CONFIG_DIR"]) / "skills"] if env.get("CLAUDE_CONFIG_DIR") else []
        found.append(home / ".claude/skills")
    else:
        found = [Path(env["CODEX_HOME"]) / "skills"] if env.get("CODEX_HOME") else []
        found += [home / ".agents/skills", home / ".codex/skills"]
    unique = []
    for path in found:
        path = path.expanduser()
        if path not in unique:
            unique.append(path)
    return unique


def default_dir(client, env=None, home=None):
    """An existing install wins, so updates never create a second copy."""
    candidates = skill_dirs(client, env, home)
    name = f"alpha-c2c-{client}"
    for directory in candidates:
        if (directory / name).exists() or (directory / name).is_symlink():
            return directory
    return candidates[0]


def detect_clients(env=None, home=None, which=shutil.which):
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    found = []
    if env.get("CLAUDE_CONFIG_DIR") or (home / ".claude").exists() or which("claude"):
        found.append("claude")
    if env.get("CODEX_HOME") or (home / ".codex").exists() or which("codex"):
        found.append("codex")
    return found


def installed_name(destination):
    skill = destination / "SKILL.md"
    match = re.search(r"^name:\s*(\S+)", skill.read_text(encoding="utf-8"), re.M) if skill.is_file() else None
    return match.group(1) if match else None


def install(client, directory, update=False, now=None):
    if client not in CLIENTS:
        raise ValueError("Unknown client.")
    name = f"alpha-c2c-{client}"
    source = ROOT / "skills" / name
    directory = directory.expanduser()
    destination = directory / name
    if destination.is_symlink():
        raise ValueError("Destination is a symlink; inspect it before replacing anything.")
    backup = None
    if destination.exists():
        if destination.is_dir() and files(source) == files(destination):
            return destination, "already installed"
        if not update:
            raise ValueError("Destination already exists with different content. Rerun with --update to back it up and replace it.")
        if not destination.is_dir() or installed_name(destination) != name:
            raise ValueError("Destination is not an %s install; inspect it, nothing was changed." % name)
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        # Outside the skills directory, so the old copy is never loaded as a second skill.
        backup = directory.parent / ".alpha-c2c-backups" / f"{name}-{stamp}"
        if backup.exists():
            raise ValueError("Backup path already exists; retry in a moment.")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".alpha-c2c-install-", dir=directory) as temporary:
        staged = Path(temporary) / name
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if files(source) != files(staged):
            raise ValueError("Copy verification failed.")
        if backup is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(backup))
        # Reserve the destination exclusively before writing, never overwrite another install.
        destination.mkdir()
        try:
            shutil.copytree(staged, destination, dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(destination)
            if backup is not None:
                shutil.move(str(backup), str(destination))
            raise
    return destination, ("updated (previous copy: %s)" % backup) if backup else "installed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=CLIENTS + ("all",),
                        help="Default: every client detected on this machine")
    parser.add_argument("--skills-dir", type=Path, help="Override the client's user skill directory")
    parser.add_argument("--update", action="store_true", help="Back up a different existing copy and replace it")
    parser.add_argument("--dry-run", action="store_true", help="Show where each skill would go")
    args = parser.parse_args(argv)
    clients = list(CLIENTS) if args.client == "all" else [args.client] if args.client else detect_clients()
    if not clients:
        parser.exit(2, "No Codex or Claude Code configuration found; pass --client.\n")
    if args.skills_dir and len(clients) > 1:
        parser.exit(2, "--skills-dir needs a single --client.\n")
    status = 0
    for client in clients:
        directory = args.skills_dir or default_dir(client)
        if args.dry_run:
            print(f"{client}: {directory / ('alpha-c2c-' + client)}")
            continue
        try:
            path, action = install(client, directory, args.update)
            print(f"{action}: {path}")
            if not shutil.which("cloudflared"):
                print("  note: `cloudflared` not found on PATH; install it before connecting ChatGPT (see README).")
        except (ValueError, OSError) as error:
            print(f"{client}: installation stopped: {error}")
            status = 2
    if not args.dry_run and status == 0:
        print("Start a new agent session to load the skill.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
