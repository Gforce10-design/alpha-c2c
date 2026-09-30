#!/usr/bin/env python3
"""Orca delivery receipts and paced reply polling. Only submit may send a message."""
import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

ASSISTANT = re.compile(r'^\s*- heading "(?:ChatGPT 답변:|ChatGPT said:|ChatGPT:)"[^\n]*$', re.M)
USER = re.compile(r'^\s*- heading "(?:내가 한 말:|You said:)"[^\n]*$', re.M)


def orca(page, *args):
    run = subprocess.run(['orca', *args, '--page', page, '--json'], capture_output=True, text=True, timeout=45)
    data = json.loads(run.stdout)
    if run.returncode or data.get('ok') is not True:
        raise RuntimeError('Browser snapshot unavailable; retain the same page and request.')
    return data['result']


def read_page(page):
    result = orca(page, 'snapshot')
    if result.get('browserPageId') != page:
        raise ValueError('Browser page identity mismatch')
    snapshot = result['snapshot']
    matches = list(ASSISTANT.finditer(snapshot))
    users = list(USER.finditer(snapshot))
    last_user = snapshot[users[-1].end():] if users else ''
    last_user = re.split(ASSISTANT, last_user, maxsplit=1)[0]
    last_user = re.split(r'^\s*- heading "(?:최신 응답|Latest response)"', last_user, maxsplit=1, flags=re.M)[0]
    latest = snapshot[matches[-1].end():] if matches else ''
    following_user = USER.search(latest)
    if following_user:
        latest = ''  # New user turn exists but has no assistant reply yet.
    # Strip the composer/footer; only the assistant turn is eligible.
    latest = re.split(r'^\s*- heading "(?:최신 응답|Latest response)"', latest, maxsplit=1, flags=re.M)[0]
    labels = [v.get('name', '') for v in result.get('refs', {}).values() if v.get('role') == 'button']
    generating = any(x in ('중지', 'Stop', 'Stop generating', '생성 중지') for x in labels)
    complete = bool(re.search(r'button "(?:응답 다시 생성|Regenerate|Try again)"', latest))
    return {'page': page, 'url': result['origin'], 'count': len(matches), 'text': latest,
            'userCount': len(users), 'lastUser': last_user, 'refs': result.get('refs', {}),
            'generating': generating, 'complete': complete}


def eligible(current, baseline, task):
    if current['page'] != baseline['page']:
        raise ValueError('Page identity changed')
    # A brand-new chat may acquire its conversation URL after the first send.
    if '/c/' in baseline['url'] and current['url'] != baseline['url']:
        raise ValueError('Conversation identity changed')
    return (current['count'] > baseline['count'] and not current['generating']
            and current['complete'] and task in current['text']
            and bool(re.search(r'\bSTATE:\s*(?:PLAN|DONE|BLOCKED)\b', current['text'])))


def wait_reply(baseline, task, timeout=600, interval=20, read=read_page, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    last = None
    errors = 0
    unposted = 0
    while clock() < deadline:
        try:
            current = read(baseline['page'])
            errors = 0
            if current.get('userCount', 0) <= baseline.get('userCount', 0):
                unposted += 1
                if unposted >= 2:
                    raise ValueError('Request not posted: inspect the composer and use submit; do not wait again.')
            else:
                unposted = 0
            digest = hashlib.sha256(current['text'].encode()).hexdigest() if eligible(current, baseline, task) else None
            if digest is not None and digest == last:
                return current
            last = digest
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            last = None
            errors += 1
            if errors >= 3:
                raise RuntimeError('Three snapshot failures; inspect this page without resending.')
        sleep(min(interval, max(0, deadline - clock())))
    raise TimeoutError('Reply still pending; reuse the same baseline and page. Do not resend.')


def normalized(text):
    return ' '.join(text.split())


def posted(current, baseline, message):
    if current['page'] != baseline['page'] or ('/c/' in baseline['url'] and current['url'] != baseline['url']):
        raise ValueError('Conversation identity changed')
    strings = re.findall(r'StaticText ("(?:[^"\\]|\\.)*")', current.get('lastUser', ''))
    body = ' '.join(json.loads(item) for item in strings)
    return current['userCount'] > baseline['userCount'] and normalized(message) in normalized(body)


def composer(page):
    expression = "JSON.stringify(Array.from(document.querySelectorAll('[role=textbox][contenteditable=true],textarea')).map(e=>e.value===undefined?e.innerText:e.value))"
    result = orca(page, 'eval', '--expression', expression)
    values = json.loads(result['result'])
    if len(values) != 1:
        raise ValueError('Expected one composer on the owned page')
    return values[0]


def owned_draft_matches(draft, message, known_draft):
    ids = lambda text: re.findall(r'^TASK_ID:\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\s*$', text, re.M)
    return (known_draft is not None and normalized(draft) == normalized(known_draft)
            and len(ids(draft)) == 1 and ids(draft) == ids(message))


def submit(baseline, message, receipt, replace_owned_draft=False, known_draft=None):
    page = baseline['page']
    current = read_page(page)
    if posted(current, baseline, message):
        return {'posted': True, 'alreadyPosted': True, 'page': page, 'url': current['url']}
    if receipt.exists():
        raise ValueError('Prior submit attempt exists but posting is unconfirmed; inspect before retrying.')
    if current['userCount'] != baseline['userCount'] or current['generating']:
        raise ValueError('Page changed since capture; reconcile before sending')
    draft = composer(page)
    if normalized(draft) and normalized(draft) != normalized(message):
        if not replace_owned_draft or not owned_draft_matches(draft, message, known_draft):
            raise ValueError('Different draft protected: replacement requires a matching previously saved draft file with the same TASK_ID. Tab inactivity is not ownership.')
    boxes = [ref for ref, meta in current['refs'].items() if meta.get('role') == 'textbox']
    if len(boxes) != 1:
        raise ValueError('Expected one accessible composer')
    orca(page, 'fill', '--element', '@' + boxes[0], '--value', message)
    if normalized(composer(page)) != normalized(message):
        raise ValueError('Composer readback differs from message file; not sent')
    current = read_page(page)
    if current['userCount'] != baseline['userCount'] or current['generating']:
        raise ValueError('Page changed while composing; not sent')
    buttons = [ref for ref, meta in current['refs'].items() if meta.get('role') == 'button' and meta.get('name') in ('보내기', 'Send', 'Send message')]
    if len(buttons) != 1:
        raise ValueError('No unique send button; not sent')
    receipt.write_text(json.dumps({'attempted': True, 'page': page, 'messageSha256': hashlib.sha256(message.encode()).hexdigest()}))
    orca(page, 'click', '--element', '@' + buttons[0])
    for _ in range(6):
        current = read_page(page)
        if posted(current, baseline, message):
            result = {'posted': True, 'page': page, 'url': current['url']}
            receipt.write_text(json.dumps(result))
            return result
        time.sleep(2)
    raise ValueError('Send clicked but posting unconfirmed. Inspect this page; do not automatically resend.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('--page', required=True)
    capture.add_argument('--output', type=Path, required=True)
    wait = sub.add_parser('wait')
    wait.add_argument('--baseline', type=Path, required=True)
    wait.add_argument('--task', required=True)
    wait.add_argument('--output', type=Path, required=True)
    wait.add_argument('--timeout', type=int, default=600, choices=range(20, 1801))
    send = sub.add_parser('submit')
    send.add_argument('--baseline', type=Path, required=True)
    send.add_argument('--message-file', type=Path, required=True)
    send.add_argument('--receipt', type=Path, required=True)
    send.add_argument('--replace-owned-draft', action='store_true')
    send.add_argument('--owned-draft-file', type=Path)
    args = p.parse_args()
    try:
        if args.action == 'submit':
            result = submit(json.loads(args.baseline.read_text()), args.message_file.read_text(), args.receipt, args.replace_owned_draft, args.owned_draft_file.read_text() if args.owned_draft_file else None)
            print(json.dumps(result)); return 0
        if args.action == 'capture':
            result = read_page(args.page)
            if result['generating']:
                raise ValueError('Page is generating; observe existing request before capturing a new baseline.')
        else:
            result = wait_reply(json.loads(args.baseline.read_text()), args.task, args.timeout)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'ok': True, 'output': str(args.output), 'page': result['page'], 'url': result['url'], 'count': result['count']}))
        return 0
    except (ValueError, RuntimeError, TimeoutError, OSError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
