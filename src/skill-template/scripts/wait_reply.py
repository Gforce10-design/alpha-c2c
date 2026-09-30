#!/usr/bin/env python3
"""Orca delivery receipts and paced reply polling. Only submit may send a message."""
import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

ASSISTANT = re.compile(r'^\s*- heading "(?:ChatGPT 답변:|ChatGPT said:|ChatGPT:)"[^\n]*$', re.M)
USER = re.compile(r'^\s*- heading "(?:내가 한 말:|You said:)"[^\n]*$', re.M)
# Connector (app) mentions render as chips in the composer and as pills in the posted
# message; they are attachments, not request text or someone's draft.
MENTION = '[data-prompt-link-href^="app://"],[app-mention-name]'
WITHOUT_MENTIONS = """const withoutMentions=e=>{
  const found=e.querySelectorAll?Array.from(e.querySelectorAll(%s)):[];
  if(!found.length) return {text:e.innerText,mentions:[]};
  const names=found.map(m=>m.getAttribute('app-mention-display-name')||m.innerText.trim());
  const copy=e.cloneNode(true); copy.querySelectorAll(%s).forEach(m=>m.remove());
  const host=document.createElement('div');
  host.style.cssText='position:fixed;left:-100000px;top:0;width:'+(e.clientWidth||600)+'px';
  host.appendChild(copy); document.body.appendChild(host);
  const text=copy.innerText; host.remove(); return {text,mentions:names};
};""" % (json.dumps(MENTION), json.dumps(MENTION))
POSTED_USER_EXPRESSION = """JSON.stringify((()=>{
  %s
  let nodes=Array.from(document.querySelectorAll('[data-user-message-bubble]'));
  if(!nodes.length) nodes=Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
  const bubble=nodes.at(-1); if(!bubble) return null;
  const e=bubble.querySelector('[data-markdown-text-tone="user-message"],.whitespace-pre-wrap')||bubble;
  const owner=bubble.closest('[data-message-id],[data-turn-id]');
  const read=withoutMentions(e);
  return {body:read.text,mentions:read.mentions,id:owner?(owner.getAttribute('data-message-id')||owner.getAttribute('data-turn-id')):null};
})())""" % WITHOUT_MENTIONS
COMPOSER_EXPRESSION = """JSON.stringify((()=>{
  %s
  return Array.from(document.querySelectorAll('[role=textbox][contenteditable=true],textarea'))
    .map(e=>e.value===undefined?withoutMentions(e):{text:e.value,mentions:[]});
})())""" % WITHOUT_MENTIONS
# Put the caret before any connector chip so inserted text never replaces it.
CARET_START_EXPRESSION = """(()=>{
  const e=document.querySelector('[role=textbox][contenteditable=true]');
  const first=e&&e.querySelector('p'); if(!first) return 'no-composer';
  e.focus(); const r=document.createRange(); r.setStart(first,0); r.collapse(true);
  const s=getSelection(); s.removeAllRanges(); s.addRange(r); return 'caret-start';
})()"""


# Real keystrokes aimed at another app can land in an autofocused ChatGPT composer
# of an owned tab, and the new-chat draft is shared by every chatgpt.com tab.
# Block trusted keyboard/IME/paste/drop input on owned pages; tool fills still work.
GUARD_EVENTS = ['keydown', 'keypress', 'keyup', 'beforeinput', 'input', 'compositionstart',
                'compositionupdate', 'compositionend', 'paste', 'drop']
GUARD_EXPRESSION = """JSON.stringify((()=>{
  if(!window.__c2cGuardInstalled){
    window.__c2cGuardInstalled=true;
    const block=e=>{if(e.isTrusted&&window.__c2cGuard!==false){e.preventDefault();e.stopImmediatePropagation();}};
    for(const t of %s) window.addEventListener(t,block,true);
  }
  window.__c2cGuard=__ENABLED__;
  if(__ENABLED__&&document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  const boxes=Array.from(document.querySelectorAll('[role=textbox][contenteditable=true],textarea'));
  return {guarded:window.__c2cGuard,url:location.href,drafts:boxes.map(e=>e.value===undefined?e.innerText:e.value)};
})())""" % json.dumps(GUARD_EVENTS)


class TransientEmptyPage(RuntimeError):
    """Same page temporarily has no document; never a posting/reply receipt."""


class ComposerText(str):
    """Composer text without connector chips; the chip names ride along."""

    def __new__(cls, text, mentions=()):
        value = super().__new__(cls, text)
        value.mentions = list(mentions)
        return value


class ProtectedDraft(ValueError):
    """A draft this helper cannot prove it owns; kept verbatim for the owner."""

    def __init__(self, message, draft):
        super().__init__(message)
        self.draft = draft


def orca(page, *args):
    run = subprocess.run(['orca', *args, '--page', page, '--json'], capture_output=True, text=True, timeout=45)
    data = json.loads(run.stdout)
    if run.returncode or data.get('ok') is not True:
        raise RuntimeError('Browser snapshot unavailable; retain the same page and request.')
    return data['result']


def guard_input(page, enabled=True):
    expression = GUARD_EXPRESSION.replace('__ENABLED__', 'true' if enabled else 'false')
    return json.loads(orca(page, 'eval', '--expression', expression)['result'])


def keep_guarded(page):
    """Re-arm after reloads; a failed attempt is never a posting/reply verdict."""
    try:
        return guard_input(page)
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def open_tab(url, worktree, timeout=15, interval=0.1, run=subprocess.run,
             guard=guard_input, sleep=time.sleep, clock=time.monotonic):
    """Create an owned tab and guard it as soon as its composer exists."""
    created = run(['orca', 'tab', 'create', '--url', url, '--worktree', worktree, '--json'],
                  capture_output=True, text=True, timeout=45)
    data = json.loads(created.stdout)
    if created.returncode or data.get('ok') is not True:
        raise RuntimeError('Tab creation failed')
    page = data['result']['browserPageId']
    deadline = clock() + timeout
    while True:
        try:
            state = guard(page)
            if state.get('guarded') is True and len(state.get('drafts', [])) == 1:
                draft = state['drafts'][0]
                return {'page': page, 'url': state.get('url'), 'guarded': True,
                        'draftAtOpen': draft, 'unknownDraft': bool(normalized(draft))}
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        if clock() >= deadline:
            raise RuntimeError('Owned tab %s opened but composer guard not confirmed; do not send from it.' % page)
        sleep(interval)


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
    user_body, user_id = None, None
    if users:
        # Accessibility snapshots can omit inert link labels and include turn controls.
        dom = orca(page, 'eval', '--expression', POSTED_USER_EXPRESSION)
        if dom.get('origin') != result['origin']:
            raise RuntimeError('Page changed during snapshot/DOM read; observe again without sending.')
        user = json.loads(dom['result'])
        if user is not None:
            user_body, user_id = user['body'], user.get('id')
            for key in ('TASK_ID', 'WORKSPACE_ID', 'ITERATION'):
                pattern = r'\b' + key + r':\s*([A-Za-z0-9_.:-]+)'
                ax = re.search(pattern, visible_text(last_user))
                body = re.search(pattern, user_body)
                if ax and (not body or ax.group(1) != body.group(1)):
                    raise RuntimeError('Latest user DOM/snapshot identity mismatch; observe again without sending.')
    return {'page': page, 'url': result['origin'], 'count': len(matches), 'text': latest,
            'userCount': len(users), 'lastUser': last_user, 'refs': result.get('refs', {}),
            'userBody': user_body, 'userMessageId': user_id,
            'emptyDocument': snapshot.strip() in ('', '(empty page)'),
            'generating': generating, 'complete': complete}


def visible_text(text):
    strings = re.findall(r'StaticText ("(?:[^"\\]|\\.)*")', text)
    return ' '.join(json.loads(item) for item in strings) if strings else text


def response_body(text):
    # Preserve tables/headings while ignoring regenerated accessibility ref IDs.
    return normalized(re.sub(r'\[ref=[^\]]+\]', '', text))


def has_task(text, task):
    return bool(re.search(r'\bTASK_ID:\s*' + re.escape(task) + r'(?![A-Za-z0-9_.:-])', text))


def user_body(current):
    if current.get('userBody') is not None:
        return current['userBody']
    return accessibility_user_body(current)


def accessibility_user_body(current):
    content = re.split(r'^\s*- button ', current.get('lastUser', ''), maxsplit=1, flags=re.M)[0]
    return visible_text(content)


def check_identity(current, baseline):
    if current['page'] != baseline['page']:
        raise ValueError('Page identity changed')
    old, new = urlsplit(baseline['url']), urlsplit(current['url'])
    empty = (not current.get('text') and not current.get('lastUser')
             and not current.get('refs') and not current.get('userBody')
             and not current.get('count') and not current.get('userCount')
             and not current.get('generating') and not current.get('complete'))
    if empty and (current['url'] in ('', 'about:blank')
                  or (current.get('emptyDocument') and current['url'] == baseline['url'])):
        raise TransientEmptyPage('Same page is temporarily empty; observe without sending or navigating.')
    if (old.scheme, old.netloc) != (new.scheme, new.netloc):
        raise ValueError('Conversation origin changed')
    if '/c/' in old.path and new.path != old.path:
        raise ValueError('Conversation identity changed')
    if '/c/' not in old.path:
        project = lambda path: re.search(r'/g/(g-p-[^/\-]+)', path)
        before, after = project(old.path), project(new.path)
        if before and (not after or before.group(1) != after.group(1)):
            raise ValueError('Project identity changed')


def new_user_turn(current, baseline):
    body = normalized(user_body(current))
    if current.get('userMessageId') and baseline.get('userMessageId'):
        return bool(body) and current['userMessageId'] != baseline['userMessageId']
    # Legacy baselines lack DOM text: improved extraction alone is not a new turn.
    if baseline.get('userBody') is None:
        changed = normalized(accessibility_user_body(current)) != normalized(accessibility_user_body(baseline))
    else:
        changed = body != normalized(user_body(baseline))
    return bool(body) and (changed or current.get('userCount', 0) > baseline.get('userCount', 0))


def eligible(current, baseline, task, message=None):
    check_identity(current, baseline)
    user = user_body(current)
    reply = visible_text(current['text'])
    request_matches = posted(current, baseline, message) if message is not None else (
        new_user_turn(current, baseline) and has_task(user, task))
    return (request_matches and not current['generating'] and current['complete']
            and response_body(current['text']) != response_body(baseline['text'])
            and has_task(reply, task)
            and bool(re.search(r'\bSTATE:\s*(?:PLAN|DONE|BLOCKED)\b', reply)))


def wait_reply(baseline, task, timeout=600, interval=20, read=read_page, sleep=time.sleep, clock=time.monotonic, message=None, protect=None):
    deadline = clock() + timeout
    last = None
    errors = 0
    identity = baseline
    while clock() < deadline:
        if protect:
            protect(baseline['page'])
        try:
            current = read(baseline['page'])
            check_identity(current, identity)
            if '/c/' not in identity['url'] and '/c/' in current['url']:
                identity = current  # Pin the first acquired conversation for subsequent reads.
            errors = 0
            digest = hashlib.sha256(response_body(current['text']).encode()).hexdigest() if eligible(current, baseline, task, message) else None
            if digest is not None and digest == last:
                return current
            last = digest
        except TransientEmptyPage:
            last = None  # A blank observation cannot count toward two stable reads.
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            last = None
            errors += 1
            if errors >= 3:
                raise RuntimeError('Three snapshot failures; inspect this page without resending.')
        sleep(min(interval, max(0, deadline - clock())))
    raise TimeoutError('Posting or reply remains unconfirmed/pending; reconcile the same page and request. Do not resend or report non-posting from a timeout.')


def normalized(text):
    return ' '.join(text.split())


def posted(current, baseline, message):
    check_identity(current, baseline)
    body = user_body(current)
    return new_user_turn(current, baseline) and normalized(message) == normalized(body)


def confirm_posting(baseline, message, receipt, timeout=120, interval=20,
                    read=read_page, sleep=time.sleep, clock=time.monotonic, protect=None):
    """Observe a prior send only; a timeout is uncertainty, never permission to resend."""
    attempt = json.loads(receipt.read_text())
    digest = hashlib.sha256(message.encode()).hexdigest()
    if attempt.get('page') != baseline['page'] or attempt.get('messageSha256') != digest:
        raise ValueError('Receipt belongs to a different page or request')
    deadline = clock() + timeout
    identity = {**baseline, 'url': attempt.get('url', baseline['url'])}
    check_identity(identity, baseline)
    errors = 0
    while True:
        if protect:
            protect(baseline['page'])
        try:
            current = read(baseline['page'])
            check_identity(current, identity)
            if '/c/' not in identity['url'] and '/c/' in current['url']:
                identity = current
            errors = 0
            if posted(current, baseline, message):
                result = {**attempt, 'posted': True, 'status': 'POSTED',
                          'page': current['page'], 'url': current['url'], 'nextAction': 'wait_same_baseline'}
                receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
                return result
        except TransientEmptyPage:
            attempt = {**attempt, 'lastObservation': 'TRANSIENT_EMPTY_PAGE'}
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            errors += 1
            if errors >= 3:
                raise RuntimeError('Three snapshot failures; retain the attempt receipt and reconcile without sending.')
        if clock() >= deadline:
            result = {**attempt, 'posted': None, 'status': 'POSTING_UNCONFIRMED',
                      'url': identity['url'], 'nextAction': 'reconcile_same_page_no_resend'}
            receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
            return result
        sleep(min(interval, max(0, deadline - clock())))


def composer(page):
    result = orca(page, 'eval', '--expression', COMPOSER_EXPRESSION)
    values = json.loads(result['result'])
    if len(values) != 1:
        raise ValueError('Expected one composer on the owned page')
    return ComposerText(values[0]['text'], values[0].get('mentions', []))


def owned_draft_matches(draft, message, known_draft):
    ids = lambda text: re.findall(r'^TASK_ID:\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\s*$', text, re.M)
    return (known_draft is not None and normalized(draft) == normalized(known_draft)
            and len(ids(draft)) == 1 and ids(draft) == ids(message))


def submit(baseline, message, receipt, replace_owned_draft=False, known_draft=None, posting_timeout=120, protect=None):
    page = baseline['page']
    if receipt.exists():
        return confirm_posting(baseline, message, receipt, timeout=posting_timeout, protect=protect)
    current = read_page(page)
    if posted(current, baseline, message):
        if receipt.exists():
            return confirm_posting(baseline, message, receipt, timeout=0)
        result = {'posted': True, 'status': 'POSTED', 'alreadyPosted': True, 'attempted': False,
                  'page': page, 'url': current['url'], 'nextAction': 'wait_same_baseline',
                  'messageSha256': hashlib.sha256(message.encode()).hexdigest()}
        receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
        return result
    if receipt.exists():
        return confirm_posting(baseline, message, receipt, timeout=posting_timeout, protect=protect)
    if new_user_turn(current, baseline) or current['generating']:
        raise ValueError('Page changed since capture; reconcile before sending')
    identity = current if '/c/' in current['url'] else baseline
    draft = composer(page)
    if normalized(draft) and normalized(draft) != normalized(message):
        if not replace_owned_draft or not owned_draft_matches(draft, message, known_draft):
            raise ProtectedDraft('Different draft protected: replacement requires a matching previously saved draft file with the same TASK_ID. Tab inactivity is not ownership.', draft)
    boxes = [ref for ref, meta in current['refs'].items() if meta.get('role') == 'textbox']
    if len(boxes) != 1:
        raise ValueError('Expected one accessible composer')
    chips = list(getattr(draft, 'mentions', []))
    if chips and normalized(draft):
        raise ValueError('Replacing a draft next to a connector chip is unsupported; not sent')
    if protect:
        guard_input(page, enabled=False)  # Only after the draft check; readback catches stray keys.
    if chips:
        # fill would wipe the chip that attaches the connector; insert before it instead.
        if orca(page, 'eval', '--expression', CARET_START_EXPRESSION).get('result') != 'caret-start':
            raise ValueError('Composer caret unavailable; not sent')
        orca(page, 'inserttext', '--text', message)
    else:
        orca(page, 'fill', '--element', '@' + boxes[0], '--value', message)
    written = composer(page)
    if normalized(written) != normalized(message):
        raise ValueError('Composer readback differs from message file; not sent')
    if list(getattr(written, 'mentions', [])) != chips:
        raise ValueError('Connector chip changed while composing; not sent')
    current = read_page(page)
    check_identity(current, identity)
    if new_user_turn(current, baseline) or current['generating']:
        raise ValueError('Page changed while composing; not sent')
    buttons = [ref for ref, meta in current['refs'].items() if meta.get('role') == 'button' and meta.get('name') in ('보내기', 'Send', 'Send message')]
    if len(buttons) != 1:
        raise ValueError('No unique send button; not sent')
    receipt.write_text(json.dumps({'attempted': True, 'status': 'ATTEMPTED', 'page': page,
                                  'url': current['url'], 'messageSha256': hashlib.sha256(message.encode()).hexdigest()}))
    orca(page, 'click', '--element', '@' + buttons[0])
    return confirm_posting(baseline, message, receipt, timeout=posting_timeout, protect=protect)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    opener = sub.add_parser('open', help='Create an owned tab and block stray keyboard input into it')
    opener.add_argument('--url', required=True)
    opener.add_argument('--worktree', required=True)
    opener.add_argument('--output', type=Path, required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('--page', required=True)
    capture.add_argument('--output', type=Path, required=True)
    wait = sub.add_parser('wait')
    wait.add_argument('--baseline', type=Path, required=True)
    wait.add_argument('--task', required=True)
    wait.add_argument('--output', type=Path, required=True)
    wait.add_argument('--message-file', type=Path)
    wait.add_argument('--timeout', type=int, default=600, choices=range(20, 1801))
    send = sub.add_parser('submit')
    send.add_argument('--baseline', type=Path, required=True)
    send.add_argument('--message-file', type=Path, required=True)
    send.add_argument('--receipt', type=Path, required=True)
    send.add_argument('--replace-owned-draft', action='store_true')
    send.add_argument('--owned-draft-file', type=Path)
    send.add_argument('--posting-timeout', type=int, default=120, choices=range(0, 601))
    reconcile = sub.add_parser('reconcile', help='Observe a prior send without filling or clicking')
    reconcile.add_argument('--baseline', type=Path, required=True)
    reconcile.add_argument('--message-file', type=Path, required=True)
    reconcile.add_argument('--receipt', type=Path, required=True)
    reconcile.add_argument('--timeout', type=int, default=120, choices=range(0, 601))
    args = p.parse_args()
    try:
        if args.action == 'open':
            result = open_tab(args.url, args.worktree)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'ok': not result['unknownDraft'], **result}, ensure_ascii=False))
            return 0 if not result['unknownDraft'] else 2
        if args.action in ('submit', 'reconcile'):
            baseline, message = json.loads(args.baseline.read_text()), args.message_file.read_text()
            if args.action == 'submit':
                result = submit(baseline, message, args.receipt, args.replace_owned_draft, args.owned_draft_file.read_text() if args.owned_draft_file else None, args.posting_timeout, protect=keep_guarded)
            else:
                result = confirm_posting(baseline, message, args.receipt, timeout=args.timeout, protect=keep_guarded)
            print(json.dumps(result)); return 0 if result.get('posted') is True else 2
        if args.action == 'capture':
            keep_guarded(args.page)
            result = read_page(args.page)
            check_identity(result, result)
            if result['generating']:
                raise ValueError('Page is generating; observe existing request before capturing a new baseline.')
        else:
            result = wait_reply(json.loads(args.baseline.read_text()), args.task, args.timeout,
                                message=args.message_file.read_text() if args.message_file else None,
                                protect=keep_guarded)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'ok': True, 'output': str(args.output), 'page': result['page'], 'url': result['url'], 'count': result['count']}))
        return 0
    except ProtectedDraft as error:
        print(json.dumps({'ok': False, 'error': str(error), 'protectedDraft': error.draft}, ensure_ascii=False))
        return 2
    except (ValueError, RuntimeError, TimeoutError, OSError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
