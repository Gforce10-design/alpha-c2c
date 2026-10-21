#!/usr/bin/env python3
"""Read-only workspace view served to the web reviewer: identity, files, search, git, execution evidence."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

READ_DEFAULT_LINES, READ_MAX_LINES, READ_MAX_BYTES = 400, 2000, 256 * 1024
SEARCH_MAX_FILE, SEARCH_PER_FILE, SEARCH_LINE = 2 * 1024 * 1024, 20, 500
DIFF_MAX_TOTAL, DIFF_BATCH_PATHS, DIFF_BATCH_BYTES = 64 * 1024 * 1024, 50, 32 * 1024
OUTPUT_KEEP, OUTPUT_LINES, OUTPUT_BYTES = 40, 200, 64 * 1024
GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', LC_ALL='C')

SENSITIVE = ['.env', '.env.*', '!.env.example', '*.pem', '*.key', '*.p12', '*.pfx', '*.jks', '*.keystore',
             'id_rsa*', 'id_ed25519*', 'id_ecdsa*', 'id_dsa*', '.ssh/', '.aws/', '.gnupg/', '.cloudflared/',
             '.npmrc', '.netrc', '_netrc', '.git-credentials', '*.keychain*', 'credentials.json',
             'service-account*.json', 'secrets.json', 'cookies.sqlite', 'Cookies', '.c2c-secrets*']
NOISE = ['.git/', 'node_modules/', 'dist/', 'build/', 'out/', '.next/', '.nuxt/', '.svelte-kit/', 'coverage/',
         '.cache/', '.turbo/', '.venv/', 'venv/', '__pycache__/', '.pytest_cache/', '.mypy_cache/', 'target/',
         '.gradle/', '.idea/', '.tooling/', '.pnpm-store/', '.DS_Store', '*.lock', 'pnpm-lock.yaml',
         'package-lock.json', 'yarn.lock']


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def compile_patterns(lines):
    """Gitignore-shaped subset: name globs at any depth, `dir/` suffix, `a/b` path globs, `!` negation."""
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        negate = line.startswith('!')
        line = line[1:] if negate else line
        out.append((negate, line.rstrip('/').lstrip('/'), line.endswith('/')))
    return out


def matches(patterns, rel, is_dir=False, fold=True):
    parts = [p for p in rel.split('/') if p]
    result = False
    for negate, pattern, dir_only in patterns:
        pat = pattern.lower() if fold else pattern
        hit = False
        for i, part in enumerate(parts):
            if dir_only and not (i < len(parts) - 1 or is_dir):
                continue
            target = '/'.join(parts[:i + 1]) if '/' in pat else part
            if fnmatch.fnmatchcase(target.lower() if fold else target, pat):
                hit = True
                break
        if hit:
            result = not negate
    return result


def workspace_id(root):
    real = os.path.realpath(root)
    if sys.platform in ('darwin', 'win32'):
        real = real.lower()
    return hashlib.sha256(real.encode('utf-8')).hexdigest()[:12]


class Workspace:
    def __init__(self, root):
        self.root = Path(os.path.realpath(root))
        if not self.root.is_dir():
            raise ToolError('WORKSPACE_NOT_FOUND', 'Workspace directory does not exist.')
        self.id = workspace_id(self.root)
        config = {}
        try:
            config = json.loads((self.root / '.c2c.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        name = config.get('name') if isinstance(config, dict) else None
        self.name = name.strip() if isinstance(name, str) and name.strip() else self.root.name
        extra = []
        try:
            extra = (self.root / '.c2cignore').read_text(encoding='utf-8').splitlines()
        except OSError:
            pass
        self.sensitive = compile_patterns(SENSITIVE + [l for l in extra if l.strip() and not l.lstrip().startswith('!')])
        self.noise = compile_patterns(NOISE)

    # --- paths -------------------------------------------------------------
    def rel(self, path):
        return path.relative_to(self.root).as_posix() if path != self.root else '.'

    def is_sensitive(self, rel, is_dir=False):
        return rel not in ('', '.') and matches(self.sensitive, rel, is_dir)

    def is_hidden(self, rel, is_dir=False):
        return self.is_sensitive(rel, is_dir) or (rel not in ('', '.') and matches(self.noise, rel, is_dir, fold=False))

    def resolve(self, value='.'):
        if not isinstance(value, str) or '\0' in value:
            raise ToolError('INVALID_PATH', 'Path must be a string without NUL bytes.')
        text = value.strip().replace('\\', '/')
        if text.startswith('workspace:/'):
            text = text[len('workspace:/'):]
        text = text.lstrip('/') or '.'
        joined = self.root / text
        probe, tail = joined, []
        while not probe.exists() and probe != probe.parent:
            tail.insert(0, probe.name)
            probe = probe.parent
        real = Path(os.path.realpath(probe)).joinpath(*tail)
        if real != self.root and self.root not in real.parents:
            raise ToolError('PATH_OUTSIDE_WORKSPACE', 'Path is outside the workspace.')
        rel = self.rel(real)
        if self.is_sensitive(rel, real.is_dir()):
            raise ToolError('ACCESS_DENIED_SENSITIVE_FILE', 'Access to sensitive files is denied.')
        return real, rel

    # --- tools -------------------------------------------------------------
    def info(self):
        files = {p.name for p in self.root.iterdir()} if self.root.is_dir() else set()
        languages, frameworks, scripts, manager = [], [], {}, None
        marks = {'package.json': 'JavaScript', 'tsconfig.json': 'TypeScript', 'pyproject.toml': 'Python',
                 'setup.py': 'Python', 'requirements.txt': 'Python', 'go.mod': 'Go', 'Cargo.toml': 'Rust',
                 'pom.xml': 'Java', 'build.gradle': 'Java', 'Gemfile': 'Ruby', 'composer.json': 'PHP'}
        for mark, lang in marks.items():
            if mark in files and lang not in languages:
                languages.append(lang)
        if 'package.json' in files:
            try:
                pkg = json.loads((self.root / 'package.json').read_text(encoding='utf-8'))
                scripts = {k: v for k, v in (pkg.get('scripts') or {}).items() if isinstance(v, str)}
                deps = {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}
                for dep, label in (('next', 'Next.js'), ('react', 'React'), ('vue', 'Vue'), ('svelte', 'Svelte'),
                                   ('express', 'Express'), ('vite', 'Vite')):
                    if dep in deps:
                        frameworks.append(label)
            except (OSError, ValueError, AttributeError):
                pass
            manager = next((m for f, m in (('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lockb', 'bun'),
                                           ('package-lock.json', 'npm')) if f in files), 'npm')
        elif 'pyproject.toml' in files or 'requirements.txt' in files:
            manager = 'uv' if 'uv.lock' in files else 'poetry' if 'poetry.lock' in files else 'pip'
        project = languages[0].lower() if languages else 'unknown'
        return {'workspaceId': self.id, 'workspaceName': self.name, 'rootAlias': 'workspace:/', 'projectType': project,
                'languages': languages, 'frameworks': frameworks, 'packageManager': manager, 'scripts': scripts,
                'git': self.git_summary()}

    def list_directory(self, path='.', depth=1, limit=200, offset=0):
        depth, limit, offset = clamp(depth, 1, 4, 1), clamp(limit, 1, 1000, 200), max(int(offset or 0), 0)
        base, rel = self.resolve(path)
        if not base.exists():
            raise ToolError('FILE_NOT_FOUND', 'Directory not found.')
        if not base.is_dir():
            raise ToolError('NOT_A_DIRECTORY', 'Path is not a directory.')
        entries, budget, queue = [], offset + limit + 2000, [(base, 1)]
        while queue and len(entries) < budget:
            current, level = queue.pop(0)
            try:
                children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower(), p.name))
            except OSError:
                continue
            for child in children:
                is_dir = child.is_dir() and not child.is_symlink()
                child_rel = self.rel(child)
                if self.is_hidden(child_rel, is_dir):
                    continue
                item = {'path': child_rel + ('/' if is_dir else ''), 'type': 'dir' if is_dir else 'file'}
                if not is_dir:
                    try:
                        item['sizeBytes'] = child.stat().st_size
                    except OSError:
                        pass
                entries.append(item)
                if is_dir and level < depth:
                    queue.append((child, level + 1))
        page = entries[offset:offset + limit]
        return {'path': rel, 'entries': page, 'total': len(entries), 'offset': offset, 'limit': limit,
                'hasMore': offset + len(page) < len(entries)}

    def read_file(self, path, start_line=None, end_line=None):
        target, rel = self.resolve(path)
        if not target.exists():
            raise ToolError('FILE_NOT_FOUND', 'File not found.')
        if not target.is_file():
            raise ToolError('NOT_A_FILE', 'Path is not a regular file.')
        size = target.stat().st_size
        if size > 20 * 1024 * 1024:
            raise ToolError('FILE_TOO_LARGE', 'File is too large to read.')
        data = target.read_bytes()
        if b'\0' in data[:8192]:
            raise ToolError('BINARY_FILE', 'Binary files are not readable.')
        lines = data.decode('utf-8', errors='replace').splitlines()
        start = max(int(start_line or 1), 1)
        end = int(end_line) if end_line else start + READ_DEFAULT_LINES - 1
        end = min(end, start + READ_MAX_LINES - 1, len(lines))
        chosen, used = [], 0
        for line in lines[start - 1:end]:
            cost = len(line.encode('utf-8')) + 1
            if used + cost > READ_MAX_BYTES and chosen:
                break
            chosen.append(line)
            used += cost
        last = start + len(chosen) - 1
        remaining = max(len(lines) - last, 0)
        return {'path': rel, 'sizeBytes': size, 'totalLines': len(lines), 'startLine': start, 'endLine': last,
                'truncated': remaining > 0, 'remainingLines': remaining,
                'nextStartLine': last + 1 if remaining else None, 'content': '\n'.join(chosen)}

    def search(self, query, path=None, glob=None, limit=50, regex=False):
        if not isinstance(query, str) or len(query) < 2:
            raise ToolError('INVALID_ARGUMENTS', 'Query must have at least 2 characters.')
        limit = clamp(limit, 1, 200, 50)
        flags = 0 if any(c.isupper() for c in query) else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error:
            raise ToolError('INVALID_ARGUMENTS', 'Invalid regular expression.') from None
        base, _ = self.resolve(path or '.')
        matches_, truncated = [], False
        candidates = [base] if base.is_file() else None
        for file in candidates or self._walk(base):
            rel = self.rel(file)
            if glob and not (fnmatch.fnmatchcase(rel, glob) or fnmatch.fnmatchcase(file.name, glob)):
                continue
            try:
                if file.stat().st_size > SEARCH_MAX_FILE:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if b'\0' in data[:8192]:
                continue
            found = 0
            for number, line in enumerate(data.decode('utf-8', errors='replace').splitlines(), 1):
                if pattern.search(line):
                    matches_.append({'path': rel, 'line': number, 'text': line[:SEARCH_LINE]})
                    found += 1
                    if len(matches_) >= limit:
                        truncated = True
                        break
                    if found >= SEARCH_PER_FILE:
                        break
            if truncated:
                break
        return {'matches': matches_, 'matchCount': len(matches_), 'truncated': truncated, 'engine': 'python'}

    def _walk(self, base):
        for current, dirs, files in os.walk(base):
            here = Path(current)
            dirs[:] = sorted(d for d in dirs if not self.is_hidden(self.rel(here / d), True))
            for name in sorted(files):
                path = here / name
                if not path.is_symlink() and not self.is_hidden(self.rel(path)):
                    yield path

    # --- git ---------------------------------------------------------------
    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, env=GIT_ENV, timeout=30)

    def git_summary(self):
        inside = self.git('rev-parse', '--is-inside-work-tree')
        if inside.returncode or inside.stdout.strip() != b'true':
            return {'isRepo': False}
        branch = self.git('rev-parse', '--abbrev-ref', 'HEAD').stdout.decode().strip() or None
        commit = self.git('rev-parse', '--short', 'HEAD')
        dirty = self.git('status', '--porcelain', '--', '.').stdout.strip() != b''
        return {'isRepo': True, 'branch': branch, 'commit': commit.stdout.decode().strip() if not commit.returncode else None,
                'dirty': dirty}

    def git_status(self):
        result = self.git('status', '--porcelain=v2', '--branch', '-z', '--', '.')
        empty = {'isRepo': False, 'branch': None, 'upstream': None, 'ahead': 0, 'behind': 0, 'staged': [],
                 'unstaged': [], 'untracked': [], 'conflicted': [], 'hidden': {'changes': 0, 'conflicts': 0}}
        if result.returncode:
            return empty
        out = dict(empty, isRepo=True)
        fields = result.stdout.decode('utf-8', errors='replace').split('\0')
        prefix = self.git('rev-parse', '--show-prefix').stdout.decode().strip()
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if not entry:
                continue
            if entry.startswith('# branch.head '):
                head = entry[len('# branch.head '):]
                out['branch'] = None if head == '(detached)' else head
            elif entry.startswith('# branch.upstream '):
                out['upstream'] = entry[len('# branch.upstream '):]
            elif entry.startswith('# branch.ab '):
                ahead, behind = entry[len('# branch.ab '):].split()
                out['ahead'], out['behind'] = int(ahead), abs(int(behind))
            elif entry[0] in '12u':
                parts = entry.split(' ', {'1': 8, '2': 9, 'u': 10}[entry[0]])
                xy, path = parts[1], parts[-1]
                if entry[0] == '2':
                    i += 1  # origPath follows as its own NUL field.
                path = path[len(prefix):] if prefix and path.startswith(prefix) else path
                if self.is_sensitive(path):
                    out['hidden']['conflicts' if entry[0] == 'u' else 'changes'] += 1
                elif entry[0] == 'u':
                    out['conflicted'].append({'path': path, 'change': 'conflict'})
                else:
                    if xy[0] != '.':
                        out['staged'].append({'path': path, 'change': CHANGE.get(xy[0], xy[0])})
                    if xy[1] != '.':
                        out['unstaged'].append({'path': path, 'change': CHANGE.get(xy[1], xy[1])})
            elif entry.startswith('? '):
                path = entry[2:]
                path = path[len(prefix):] if prefix and path.startswith(prefix) else path
                if self.is_sensitive(path):
                    out['hidden']['changes'] += 1
                else:
                    out['untracked'].append(path)
        return out

    def git_diff(self, mode='unstaged', path=None, offset=0, max_bytes=65536):
        mode_args = {'unstaged': [], 'staged': ['--cached'], 'head': ['HEAD']}
        if mode not in mode_args:
            raise ToolError('INVALID_ARGUMENTS', 'mode must be unstaged, staged or head.')
        offset, max_bytes = max(int(offset or 0), 0), clamp(max_bytes, 1024, 262144, 65536)
        scope = None
        if path:
            _, scope = self.resolve(path)
        closed = {'isRepo': False, 'mode': mode, 'totalBytes': 0, 'offset': offset, 'returnedBytes': 0,
                  'hasMore': False, 'nextOffset': None, 'diff': ''}
        listed = self.git('diff', '--name-status', '-z', '--relative', '--find-renames=1%', *mode_args[mode], '--', '.')
        if listed.returncode:
            return closed
        fields, keep, i = listed.stdout.decode('utf-8', errors='replace').split('\0'), [], 0
        while i < len(fields) and fields[i]:
            status = fields[i]
            names = fields[i + 1:i + (3 if status[0] in 'RC' else 2)]
            i += 1 + len(names)
            if any(self.is_sensitive(n) for n in names):
                continue
            if scope not in (None, '.') and not any(n == scope or n.startswith(scope.rstrip('/') + '/') for n in names):
                continue
            keep.extend(names)
        keep = list(dict.fromkeys(keep))
        chunks, total, batch, size = [], 0, [], 0
        for name in keep + [None]:
            if name is not None and len(batch) < DIFF_BATCH_PATHS and size + len(name) < DIFF_BATCH_BYTES:
                batch.append(name)
                size += len(name)
                continue
            if batch:
                result = self.git('diff', '--no-ext-diff', '--no-color', '--relative', '--find-renames=1%',
                                  *mode_args[mode], '--', *(':(literal)' + n for n in batch))
                if result.returncode:
                    return closed
                chunks.append(result.stdout)
                total += len(result.stdout)
                if total > DIFF_MAX_TOTAL:
                    return closed
            batch, size = ([name], len(name)) if name is not None else ([], 0)
        data = b''.join(chunks)
        piece = data[offset:offset + max_bytes]
        more = offset + len(piece) < len(data)
        if more and b'\n' in piece:
            piece = piece[:piece.rfind(b'\n') + 1]
        return {'isRepo': True, 'mode': mode, 'totalBytes': len(data), 'offset': offset, 'returnedBytes': len(piece),
                'hasMore': more, 'nextOffset': offset + len(piece) if more else None,
                'diff': piece.decode('utf-8', errors='replace')}


CHANGE = {'M': 'modified', 'A': 'added', 'D': 'deleted', 'R': 'renamed', 'C': 'copied', 'T': 'typechange'}


def clamp(value, low, high, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


# --- execution evidence -----------------------------------------------------
PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----')
REDACT = [
    (re.compile(r'c2c_(?:at|rt|ac|admin)_[A-Za-z0-9_-]+'), '[redacted-token]'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'), 'Bearer [redacted]'),
    (re.compile(r'(?i)\b(code|token|access_token|refresh_token)=([A-Za-z0-9._~+/-]{16,})'), r'\1=[redacted]'),
    (re.compile(r'\b[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}\b'), '[redacted-code]'),
    (re.compile(r'\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}'), '[redacted-github]'),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), '[redacted-key]'),
    (re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}'), '[redacted-slack]'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '[redacted-aws]'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'), '[redacted-google]'),
    (re.compile(r'(?i)\b(api[_-]?key|secret|password|passwd|authorization)(\s*[:=]\s*)("[^"]*"|\'[^\']*\'|\S+)'),
     r'\1\2[redacted]'),
    (re.compile(r'/Users/[^/\s:"\']+'), '/Users/[user]'),
    (re.compile(r'/home/[^/\s:"\']+'), '/home/[user]'),
    (re.compile(r'(?i)\b([A-Z]):\\Users\\[^\\\s:"\']+'), r'\1:\\Users\\[user]'),
]


def sanitize(text):
    """Return (allowed, reason, text, truncated). Private keys withhold the whole body."""
    if PRIVATE_KEY.search(text):
        return False, 'private_key', '', False
    for pattern, replacement in REDACT:
        text = pattern.sub(replacement, text)
    lines, truncated = text.splitlines(), False
    if len(lines) > OUTPUT_LINES:
        lines, truncated = lines[:OUTPUT_LINES], True
    text = '\n'.join(lines)
    if len(text.encode('utf-8')) > OUTPUT_BYTES:
        text, truncated = text.encode('utf-8')[:OUTPUT_BYTES].decode('utf-8', errors='ignore'), True
    return True, None, text + ('\n…[truncated]' if truncated else ''), truncated
