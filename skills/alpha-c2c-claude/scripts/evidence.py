#!/usr/bin/env python3
"""Run an agent-reviewed command spec after PLAN; attach complete preservation evidence."""
import argparse
import hashlib
import json
import os
import shlex
import stat
import shutil
import subprocess
import tempfile
from pathlib import Path


# Observation must not rewrite .git/index: `git diff` refreshes its stat cache even with
# optional locks off. Snapshot git commands read a private copy so the raw index bytes
# can be compared before/after.
READ_ONLY_GIT = dict(os.environ, GIT_OPTIONAL_LOCKS='0')


def git(workspace, *args, env=READ_ONLY_GIT):
    return subprocess.check_output(['git', '-C', str(workspace), *args], env=env)


def branch(workspace, env=READ_ONLY_GIT):
    result = subprocess.run(['git', '-C', str(workspace), 'symbolic-ref', '-q', 'HEAD'],
                            capture_output=True, env=env)
    if result.returncode == 1:
        return {'detached': True}
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return {'symbolicRef': result.stdout.decode().strip()}


def index_path(workspace):
    return Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())


def index_file(path):
    if not path.exists():
        return {'path': str(path), 'exists': False}
    data = path.read_bytes()
    return {'path': str(path), 'exists': True, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def snapshot(workspace):
    real = index_path(workspace)
    raw = index_file(real)  # Before any command that could touch it.
    with tempfile.TemporaryDirectory() as private:
        copy = Path(private) / 'index'
        if real.exists():
            shutil.copy2(real, copy)
        state = observe(workspace, dict(READ_ONLY_GIT, GIT_INDEX_FILE=str(copy)))
    if index_file(real) != raw:
        raise RuntimeError('.git/index changed while observing; another process is using this repository')
    return {**state, 'gitIndexFile': raw}


def observe(workspace, env):
    git_ = lambda *args: git(workspace, *args, env=env)
    files = {}
    names = git_('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
    for raw in sorted(set(names) - {b''}):
        name = os.fsdecode(raw)
        path = workspace / name
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                value = {'symlink': os.readlink(path)}
            elif stat.S_ISREG(mode):
                value = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': stat.S_IMODE(mode)}
            elif stat.S_ISDIR(mode):
                raise ValueError('Submodule/directory tracked content needs a repository-specific verifier')
            else:
                raise ValueError('Unsupported source file type')
        except FileNotFoundError:
            value = {'missing': True}
        files[name] = value
    return {'head': git_('rev-parse', 'HEAD').decode().strip(),
            'index': git_('ls-files', '--stage', '-z').decode(),
            'status': git_('status', '--porcelain=v1', '-z', '--untracked-files=all').decode(),
            'stagedDiff': git_('diff', '--cached', '--binary', '--no-ext-diff').decode(),
            'unstagedDiff': git_('diff', '--binary', '--no-ext-diff').decode(), 'files': files,
            'branch': branch(workspace, env),
            'refs': git_('for-each-ref', '--format=%(refname)%00%(objectname)').decode()}


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + '\n')


def require_plan(session, task, iteration, plan):
    cp = session.get('session', {}).get('checkpoint', {})
    if (session.get('ok') is not True or cp.get('taskId') != task or cp.get('iteration') != iteration
            or cp.get('protocolState') not in ('PLAN_RECEIVED', 'EXECUTING')
            or not plan.is_file() or not plan.read_text().strip()):
        raise ValueError('Require the matching received PLAN checkpoint and saved actual plan before execution')


def record(bridge, workspace, task, iteration, path, command, code, changed):
    result = subprocess.run([bridge, 'record', '-w', str(workspace), '--task', task,
        '--iteration', str(iteration), '--changed-files', str(changed), '--tests', 'See attached actual output',
        '--exit-status', 'ok' if code == 0 else 'failed', '--command', command,
        '--output-file', str(path), '--exit-code', str(code)], capture_output=True, text=True)
    receipt = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    save(path.with_suffix(path.suffix + '.record.json'), receipt)
    if result.returncode:
        raise RuntimeError('Evidence record failed; inspect receipt before any retry (outcome may be uncertain)')


def execute(workspace, output, commands):
    if not isinstance(commands, list) or not commands or any(not isinstance(c, list) or not c or
            any(not isinstance(a, str) or not a or '\0' in a for a in c) for c in commands):
        raise ValueError('Spec must be a nonempty JSON array of reviewed argv arrays')
    output.mkdir(parents=True, exist_ok=False)
    before = snapshot(workspace)
    save(output / 'before.json', before)
    results = []
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for i, argv in enumerate(commands, 1):
        path = output / f'command-{i}.txt'
        with path.open('w') as log:
            log.write('ARGV: ' + json.dumps(argv) + '\n'); log.flush()
            try:
                process = subprocess.run(argv, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT)
                code = process.returncode
            except OSError as error:
                log.write(f'Launch failed: {type(error).__name__}\n'); code = 127
            log.write(f'\nEXIT_CODE: {code}\n')
        results.append({'argv': argv, 'exitCode': code, 'output': str(path)})
    after = snapshot(workspace)
    save(output / 'after.json', after)
    changed = [k for k in before if before[k] != after[k]]
    comparison = {'identical': not changed, 'changedSections': changed,
        'beforeSha256': hashlib.sha256((output/'before.json').read_bytes()).hexdigest(),
        'afterSha256': hashlib.sha256((output/'after.json').read_bytes()).hexdigest()}
    save(output / 'comparison.json', comparison)
    save(output / 'commands.json', results)
    return results, comparison


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--iteration', type=int, required=True)
    p.add_argument('--plan-file', type=Path, required=True)
    p.add_argument('--spec', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--bridge', default='code-with-chatgpt')
    a = p.parse_args()
    workspace, output = a.workspace.resolve(), a.output.resolve()
    try:
        if output == workspace or workspace in output.parents:
            raise ValueError('Evidence output must be outside the tested workspace')
        session = json.loads(subprocess.check_output([a.bridge, 'session', 'get', '-w', str(workspace), '--json']))
        require_plan(session, a.task, a.iteration, a.plan_file)
        results, comparison = execute(workspace, output, json.loads(a.spec.read_text()))
        changed = int(not comparison['identical'])
        for item in results:
            record(a.bridge, workspace, a.task, a.iteration, Path(item['output']), shlex.join(item['argv']), item['exitCode'], changed)
        for name in ('before.json', 'after.json', 'comparison.json'):
            record(a.bridge, workspace, a.task, a.iteration, output / name,
                   f'evidence.py {name} (HEAD, branch symbolic ref, refs, index entries, raw .git/index SHA256, status, diffs, source SHA256)', changed, changed)
        ok = not changed and all(item['exitCode'] == 0 for item in results)
        print(json.dumps({'ok': ok, 'output': str(output), 'preserved': not changed, 'commands': results}))
        return 0 if ok else 1
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
