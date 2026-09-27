#!/usr/bin/env python3
"""Install a standalone skill without changing bridge or account settings."""
from pathlib import Path
import argparse
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def install(client, directory):
    if client not in {"codex", "claude"}:
        raise ValueError("Unknown client.")
    name = f"alpha-c2c-{client}"
    source = ROOT / "skills" / name
    directory = directory.expanduser()
    destination = directory / name
    if destination.is_symlink():
        raise ValueError("Destination is a symlink; inspect it before replacing anything.")
    if destination.exists():
        if destination.is_dir() and files(source) == files(destination):
            return destination, "already installed"
        raise ValueError("Destination already exists with different content. Back it up and remove or rename it before installing.")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".alpha-c2c-install-", dir=directory) as temporary:
        staged = Path(temporary) / name
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if files(source) != files(staged):
            raise ValueError("Copy verification failed.")
        # Reserve the destination exclusively before writing, never overwrite another install.
        destination.mkdir()
        try:
            shutil.copytree(staged, destination, dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(destination)
            raise
    return destination, "installed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "claude"), required=True)
    parser.add_argument("--skills-dir", type=Path, help="Override the client's user skill directory")
    args = parser.parse_args()
    default = Path.home() / (".agents/skills" if args.client == "codex" else ".claude/skills")
    try:
        path, action = install(args.client, args.skills_dir or default)
        print(f"{action}: {path}")
    except (ValueError, OSError) as error:
        parser.exit(2, f"Installation stopped: {error}\n")
