#!/usr/bin/env python3
"""Orca, Chrome DevTools or agent-driven delivery receipts and paced reply polling. Only submit may send a message."""
import argparse
import hashlib
import importlib.util
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
  if(!found.length&&e.isConnected) return {text:e.innerText,mentions:[]};
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
# Read authored posted units in document order, never the composer or tool payloads.
POSTED_TURNS_EXPRESSION = r"""JSON.stringify((()=>{
  %s
  const latest=JSON.parse(eval(%s));
  let roots=Array.from(document.querySelectorAll('[data-chatgpt-search-unit-key][data-chatgpt-search-message-ids]'));
  if(!roots.length) roots=Array.from(document.querySelectorAll('[data-message-author-role="user"],[data-message-author-role="assistant"]'));
  const turns=roots.filter(e=>!e.closest('[role=textbox],form,[data-tool-call],[data-tool-result]')).map(e=>{
    const key=e.getAttribute('data-chatgpt-search-unit-key')||'';
    const role=e.getAttribute('data-message-author-role')||(key.endsWith(':user')?'user':key.endsWith(':assistant')?'assistant':null);
    const ids=Array.from(new Set((e.getAttribute('data-chatgpt-search-message-ids')||e.getAttribute('data-message-id')||e.getAttribute('data-turn-id')||'').trim().split(/\s+/).filter(Boolean)));
    const selector=role==='user'?'[data-markdown-text-tone="user-message"],.whitespace-pre-wrap':'[data-message-content],.markdown,.text-size-chat.whitespace-pre-wrap';
    let contents=role==='assistant'?Array.from(e.querySelectorAll('.markdown,[class]')).filter(n=>
      n.matches('.markdown')||Array.from(n.classList).some(token=>token.startsWith('MarkdownRoot-'))):[];
    if(!contents.length) contents=Array.from(e.querySelectorAll(selector));
    contents=contents.filter(n=>!n.closest||!n.closest('[data-tool-call],[data-tool-result],[data-message-author-role="tool"]'));
    contents=contents.filter(n=>!contents.some(other=>other!==n&&other.contains(n)));
    if(!contents.length){const one=e.querySelector(selector);if(one) contents=[one];}
    if(!contents.length||!role) return null;
    const reads=contents.map(content=>{
      const copy=content.cloneNode(true);
      copy.querySelectorAll('[data-tool-call],[data-tool-result],[data-message-author-role="tool"],button,h4[data-conversation-role]').forEach(n=>n.remove());
      const read=withoutMentions(copy);
      // Preserve visible body. Only real simple inline-code DOM nodes can
      // supply their lost single-backtick markers for authored request matching.
      const marked=role==='user'?copy.cloneNode(true):null;
      const codes=marked?Array.from(marked.querySelectorAll('code')):[];
      const supported=codes.length>0&&codes.length<=64&&codes.every(n=>
        !n.closest('pre')&&n.children.length===0&&n.textContent.length>0&&!/[`\r\n]/.test(n.textContent));
      let authored={};
      if(supported){
        codes.forEach(n=>n.replaceWith(document.createTextNode('`'+n.textContent+'`')));
        authored={inlineCodeBodyVersion:'user-inline-code-single-backtick.v1',
          inlineCodeMarkCount:codes.length,inlineCodeMarkedBody:withoutMentions(marked).text};
      }
      copy.querySelectorAll('blockquote,pre,code').forEach(n=>n.remove());
      return {...read,...authored,protocol:withoutMentions(copy).text};
    });
    const read={text:reads.map(r=>r.text).join('\n'),mentions:reads.flatMap(r=>r.mentions)},state=e.closest('[data-talvt-turn-state]');
    const complete=role==='assistant'&&(state?state.getAttribute('data-talvt-turn-state')==='complete':Array.from(e.querySelectorAll('button')).some(b=>['응답 다시 생성','Regenerate','Try again'].includes(b.getAttribute('aria-label')||b.innerText)));
    return {role,body:read.text,...(role==='user'&&reads.length===1&&reads[0].inlineCodeBodyVersion?{
      inlineCodeBodyVersion:reads[0].inlineCodeBodyVersion,inlineCodeMarkCount:reads[0].inlineCodeMarkCount,
      inlineCodeMarkedBody:reads[0].inlineCodeMarkedBody}:{}),
      protocolSegments:reads.map(r=>r.protocol),mentions:read.mentions,id:ids.length===1?ids[0]:null,ids,key,complete};
  }).filter(Boolean);
  return {...(latest||{}),turns};
})())""" % (WITHOUT_MENTIONS,json.dumps(POSTED_USER_EXPRESSION))

COMPOSER_EXPRESSION = """JSON.stringify((()=>{
  %s
  return Array.from(document.querySelectorAll('form [role=textbox][contenteditable=true],form textarea'))
    .map(e=>({... (e.value===undefined?withoutMentions(e):{text:e.value,mentions:[]}),accessibleName:e.getAttribute('aria-label')||''}));
})())""" % WITHOUT_MENTIONS
# Put the caret before any connector chip so inserted text never replaces it.
CARET_START_EXPRESSION = """(()=>{
  const e=document.querySelector('form [role=textbox][contenteditable=true]');
  const first=e&&e.querySelector('p'); if(!first) return 'no-composer';
  e.focus(); const r=document.createRange(); r.setStart(first,0); r.collapse(true);
  const s=getSelection(); s.removeAllRanges(); s.addRange(r); return 'caret-start';
})()"""

# Orca can return `clicked` for a disabled button without dispatching a click.
# Accessibility refs omit disabled metadata; inspect the live composer form and
# use its semantic selector, rather than assuming a tool acknowledgement sent it.
SEND_READY_EXPRESSION = """JSON.stringify((()=>{
  const boxes=Array.from(document.querySelectorAll('form [role=textbox][contenteditable=true],form textarea'));
  if(boxes.length!==1) return {ready:false,reason:'composer-count'};
  const form=boxes[0].closest('form');
  if(!form) return {ready:false,reason:'composer-form-unavailable'};
  const names=['보내기','Send','Send message'];
  const buttons=Array.from(form.querySelectorAll('button')).filter(e=>names.includes(e.getAttribute('aria-label')));
  if(buttons.length!==1) return {ready:false,reason:'send-button-count'};
  const e=buttons[0],r=e.getBoundingClientRect();
  if(e.disabled||e.matches(':disabled')||e.getAttribute('aria-disabled')==='true')
    return {ready:false,reason:'send-disabled'};
  const style=getComputedStyle(e);
  if(r.width<=0||r.height<=0||style.visibility!=='visible'||style.display==='none'||style.pointerEvents==='none')
    return {ready:false,reason:'send-hidden'};
  const top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
  if(!top||!(top===e||e.contains(top))) return {ready:false,reason:'send-obscured'};
  const selector='form button[aria-label='+JSON.stringify(e.getAttribute('aria-label'))+']';
  if(document.querySelectorAll(selector).length!==1) return {ready:false,reason:'send-selector-ambiguous'};
  return {ready:true,selector};
})())"""


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
  const boxes=Array.from(document.querySelectorAll('form [role=textbox][contenteditable=true],form textarea'));
  return {guarded:window.__c2cGuard,url:location.href,drafts:boxes.map(e=>e.value===undefined?e.innerText:e.value)};
})())""" % json.dumps(GUARD_EVENTS)

# Agent-driven delivery: the agent runs this read-only expression with its own
# browser tool and saves the returned JSON string. No AX snapshot exists here, so
# the AX/DOM identity cross-check of read_page is not available in this mode.
OBSERVATION_SCHEMA = 'c2c-dom-observation-v1'
STOP_NAMES = ['중지', 'Stop', 'Stop generating', '생성 중지', 'Stop streaming', '스트리밍 중지']
OBSERVE_EXPRESSION = """JSON.stringify((()=>{
  const posted=JSON.parse(%s);
  const composer=JSON.parse(%s);
  const sendReady=JSON.parse(%s);
  const stop=%s;
  const generating=!!document.querySelector('[data-testid="stop-button"]')||
    Array.from(document.querySelectorAll('button')).some(b=>stop.includes(b.getAttribute('aria-label')||''));
  return {schema:%s,url:location.href,observedAt:Date.now(),
    emptyDocument:!document.body||!document.body.innerText.trim(),generating,posted,composer,sendReady};
})())""" % (POSTED_TURNS_EXPRESSION, COMPOSER_EXPRESSION, SEND_READY_EXPRESSION,
            json.dumps(STOP_NAMES, ensure_ascii=False), json.dumps(OBSERVATION_SCHEMA))
AGENT_PAGE_PREFIX = 'agent:'
AGENT_MIN_GAP_SECONDS = 10
# Chrome DevTools pages: a user-launched local Chrome; DOM observation replaces the AX snapshot.
CDP_PAGE_PREFIX = 'cdp:'
CDP_POINTER = {}
CDP_FOCUS_EXPRESSION = """(()=>{
  const boxes=Array.from(document.querySelectorAll('form [role=textbox][contenteditable=true],form textarea'));
  if(boxes.length!==1) return 'no-composer';
  const e=boxes[0]; e.focus();
  if(%s){
    if(e.value!==undefined) e.select();
    else {const r=document.createRange(); r.selectNodeContents(e); const s=getSelection(); s.removeAllRanges(); s.addRange(r);}
  }
  return 'focused';
})()"""
CDP_HIT_EXPRESSION = """JSON.stringify((()=>{
  const found=document.querySelectorAll(%s); if(found.length!==1) return {ok:false};
  const e=found[0]; e.scrollIntoView({block:'nearest'});
  const r=e.getBoundingClientRect(), x=r.left+r.width/2, y=r.top+r.height/2, top=document.elementFromPoint(x,y);
  if(r.width<=0||r.height<=0||!top||!(top===e||e.contains(top))) return {ok:false};
  if(e.disabled||e.getAttribute('aria-disabled')==='true') return {ok:false};
  return {ok:true,x,y};
})())"""


class TransientEmptyPage(RuntimeError):
    """Same page temporarily has no document; never a posting/reply receipt."""


class ComposerText(str):
    """Composer text without connector chips; the chip names ride along."""

    def __new__(cls, text, mentions=(), accessible_name=None):
        value = super().__new__(cls, text)
        value.mentions = list(mentions)
        value.accessible_name = accessible_name
        return value


class ProtectedDraft(ValueError):
    """A draft this helper cannot prove it owns; kept verbatim for the owner."""

    def __init__(self, message, draft):
        super().__init__(message)
        self.draft = draft


def _cdp():
    spec = importlib.util.spec_from_file_location('c2c_cdp', Path(__file__).with_name('cdp.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cdp_click(session, x, y):
    for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
        session.call('Input.dispatchMouseEvent', type=kind, x=x, y=y, button='left' if kind != 'mouseMoved' else 'none',
                     clickCount=1 if kind != 'mouseMoved' else 0)


def _cdp_lines(session, text):
    """Native insertion with Shift+Enter separators, as on the Orca path."""
    for index, line in enumerate(text.split('\n')):
        if index:
            for kind in ('keyDown', 'keyUp'):
                session.call('Input.dispatchKeyEvent', type=kind, key='Enter', code='Enter',
                             windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, modifiers=8)
        if line:
            session.call('Input.insertText', text=line)


def cdp_browser(page, *args):
    """The Orca verbs this helper uses, over Chrome DevTools Protocol."""
    module = _cdp()
    try:
        session = module.Session(page[len(CDP_PAGE_PREFIX):])
    except OSError as error:
        # Socket refusals and timeouts are transient, like an Orca CLI timeout.
        raise module.CDPError('DevTools transport failed; observe again without sending') from error
    try:
        return _cdp_verb(session, page, *args)
    except OSError as error:
        raise module.CDPError('DevTools transport failed; observe again without sending') from error
    finally:
        session.close()


def _cdp_verb(session, page, *args):
    verb, rest = args[0], list(args[1:])
    action = rest.pop(0) if verb == 'mouse' else None
    options = dict(zip(rest[0::2], rest[1::2]))
    element = options.get('--element')
    if element is not None and verb in ('focus', 'fill') and element != '@composer':
        raise ValueError('Unknown element reference on a CDP page')
    if verb == 'eval':
        wrapped = '(()=>{const value=(%s);return JSON.stringify({origin:location.href,value});})()' % options['--expression']
        out = json.loads(session.evaluate(wrapped))
        return {'result': out.get('value'), 'origin': out['origin']}
    if verb == 'mouse':
        if action == 'move':
            CDP_POINTER[page] = (float(options['--x']), float(options['--y']))
            x, y = CDP_POINTER[page]
            session.call('Input.dispatchMouseEvent', type='mouseMoved', x=x, y=y)
        elif action in ('down', 'up') and page in CDP_POINTER:
            x, y = CDP_POINTER[page]
            session.call('Input.dispatchMouseEvent', type='mousePressed' if action == 'down' else 'mouseReleased',
                         x=x, y=y, button='left', clickCount=1)
        else:
            raise ValueError('Unsupported mouse action on a CDP page')
        return {}
    if verb == 'focus':
        if session.evaluate(CDP_FOCUS_EXPRESSION % 'false') != 'focused':
            raise ValueError('Composer unavailable for focus; not sent')
        return {}
    if verb == 'keypress':
        if options.get('--key') != 'Shift+Enter':
            raise ValueError('Only Shift+Enter is supported on a CDP page')
        _cdp_lines(session, '\n')
        return {}
    if verb == 'inserttext':
        session.call('Input.insertText', text=options['--text'])
        return {}
    if verb == 'fill':
        # Replace only the editor contents; the caller reads the composer back.
        if session.evaluate(CDP_FOCUS_EXPRESSION % 'true') != 'focused':
            raise ValueError('Composer unavailable for fill; not sent')
        _cdp_lines(session, options['--value'])
        return {}
    if verb == 'click':
        hit = json.loads(session.evaluate(CDP_HIT_EXPRESSION % json.dumps(element)))
        if not hit.get('ok'):
            raise ValueError('Click target unavailable or obscured; no click dispatched')
        _cdp_click(session, hit['x'], hit['y'])
        return {}
    if verb == 'screenshot':
        session.call('Page.captureScreenshot')
        return {}
    raise ValueError('Unsupported browser operation on a CDP page: ' + verb)


def orca(page, *args):
    if page.startswith(AGENT_PAGE_PREFIX):
        raise ValueError('Agent-driven page; use the agent-* subcommands, never Orca')
    if page.startswith(CDP_PAGE_PREFIX):
        return cdp_browser(page, *args)
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
             guard=guard_input, sleep=time.sleep, clock=time.monotonic, backend='orca'):
    """Create an owned tab and guard it as soon as its composer exists."""
    if backend == 'cdp':
        page = CDP_PAGE_PREFIX + _cdp().new_target(url)['id']
    else:
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
    if page.startswith(CDP_PAGE_PREFIX):
        observation = json.loads(orca(page, 'eval', '--expression', OBSERVE_EXPRESSION)['result'])
        if not isinstance(observation, dict) or observation.get('schema') != OBSERVATION_SCHEMA:
            raise RuntimeError('Browser snapshot unavailable; retain the same page and request.')
        return page_from_observation(observation, page)
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
    user_body, user_id, turns = None, None, None
    if snapshot.strip() not in ('', '(empty page)') or result.get('refs'):
        # Accessibility snapshots can omit inert link labels and include turn controls.
        dom = orca(page, 'eval', '--expression', POSTED_TURNS_EXPRESSION)
        if dom.get('origin') != result['origin']:
            raise RuntimeError('Page changed during snapshot/DOM read; observe again without sending.')
        user = json.loads(dom['result'])
        if user is not None:
            user_body, user_id = user.get('body'), user.get('id')
            turns = user.get('turns')
            if user_body is None and turns:
                latest_user = next((t for t in reversed(turns) if t['role'] == 'user'), None)
                if latest_user:
                    user_body, user_id = latest_user['body'], latest_user.get('id')
            for key in ('TASK_ID', 'WORKSPACE_ID', 'ITERATION', 'REQUEST_ID'):
                pattern = r'\b' + key + r':\s*([A-Za-z0-9_.:-]+)'
                ax = re.search(pattern, visible_text(last_user))
                body = re.search(pattern, user_body or '')
                if ax and (not body or ax.group(1) != body.group(1)):
                    raise RuntimeError('Latest user DOM/snapshot identity mismatch; observe again without sending.')
    if turns:
        dom_reply = next((t for t in reversed(turns) if t['role'] == 'assistant'), None)
        if dom_reply and latest:
            for key in ('TASK_ID', 'ITERATION', 'WORKSPACE_ID', 'REQUEST_ID'):
                ax_values, dom_values = marker_values(visible_text(latest), key), marker_values(dom_reply['body'], key)
                if ax_values and ax_values != dom_values:
                    raise RuntimeError('Assistant DOM/snapshot identity mismatch; observe again without sending')
    return {'page': page, 'url': result['origin'], 'count': len(matches), 'text': latest,
            'userCount': len(users), 'lastUser': last_user, 'refs': result.get('refs', {}),
            'userBody': user_body, 'userMessageId': user_id,
            **({'postedTurns': turns} if turns is not None else {}),
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


def matching_posted_body(turn, message):
    """Exact existing comparison, or markers reconstructed from qualified DOM.

    Never remove backticks from requests, drafts, arbitrary text or code blocks.
    Raw visible body remains available separately from authored reconstruction.
    """
    body = turn.get('body', '')
    if normalized(body) == normalized(message):
        return body, 'visible-body'
    count = turn.get('inlineCodeMarkCount')
    marked = turn.get('inlineCodeMarkedBody')
    if (turn.get('role') == 'user' and
            turn.get('inlineCodeBodyVersion') == 'user-inline-code-single-backtick.v1' and
            type(count) is int and 1 <= count <= 64 and isinstance(marked, str) and
            normalized(marked) == normalized(message)):
        return marked, 'dom-single-backtick-inline-code'
    return None


def rendered_request(current, message):
    """Unique authored request, independent of whether it predates capture."""
    turns = current.get('postedTurns')
    if turns is None:
        return current if normalized(user_body(current)) == normalized(message) else None
    matches = [i for i, t in enumerate(turns) if t.get('role') == 'user'
               and matching_posted_body(t, message) is not None]
    # An ambiguous body/ID must inhibit input, not silently fall back to latest AX.
    ids = [i for t in turns for i in t.get('ids', [t.get('id')]) if i]
    if len(matches) > 1 or len(ids) != len(set(ids)):
        raise ValueError('Ambiguous posted request or reused message identity; observe without sending')
    if not matches:
        # Even unsupported/empty whole-turn extraction cannot authorize duplicate input.
        if (normalized(user_body(current)) == normalized(message)
                or normalized(accessibility_user_body(current)) == normalized(message)):
            return {**current, 'unsupportedPostedStructure': True}
        return None
    index = matches[0]
    request = turns[index]
    following = []
    for turn in turns[index + 1:]:
        if turn.get('role') == 'user':
            break
        if turn.get('role') == 'assistant':
            following.append(turn)
    reply = following[0] if len(following) == 1 else None
    authored, body_mode = matching_posted_body(request, message)
    return {**current, 'userBody': authored, 'visibleUserBody': request['body'],
            'postedBodyMode': body_mode, 'userMessageId': request.get('id'),
            'lastUser': request['body'], 'text': reply['body'] if reply else '',
            'responseMessageId': reply.get('id') if reply else None,
            'protocolSegments': reply.get('protocolSegments') if reply else None,
            'complete': bool(reply and reply.get('complete')),
            'associatedDOM': True, 'rawResponseSnapshot': current['text'],
            'selectedRequestIndex': index}


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def validated_generation(baseline, message, proof=None, attempt=None):
    """Descriptive preparation correlation, not permission or server attestation."""
    archived = attempt.get('generationProof') if attempt else None
    if attempt is not None and proof is not None and archived is None:
        raise ValueError('Existing receipt has no original generation proof; retrospective proof graft refused')
    supplied = json.loads(proof.read_text()) if isinstance(proof, Path) else proof
    if archived is not None:
        if attempt.get('generationProofSha256') != canonical_sha(archived):
            raise ValueError('Archived generation proof hash mismatch')
        if supplied is not None and supplied != archived:
            raise ValueError('Generation proof differs from original attempted request')
        supplied = archived
    if supplied is None:
        return None
    producer = Path(__file__).with_name('c2c.py')
    expected = protocol_header(message)
    identity = {key: next(iter(expected[key])) for key in ('TASK_ID','WORKSPACE_ID','ITERATION','REQUEST_ID')
                if key in expected and len(expected[key]) == 1}
    nonce = identity.get('REQUEST_ID', '')
    if (not isinstance(supplied, dict) or supplied.get('schema') != 'c2c-prepared-request-v1'
            or supplied.get('status') != 'prepared'
            or supplied.get('producer') != '__SKILL_NAME__/scripts/c2c.py'
            or supplied.get('producerSha256') != hashlib.sha256(producer.read_bytes()).hexdigest()
            or supplied.get('messageSha256') != hashlib.sha256(message.encode()).hexdigest()
            or supplied.get('baselineSha256') != canonical_sha(baseline)
            or supplied.get('page') != baseline['page'] or supplied.get('url') != baseline['url']
            or len(identity) != 4 or supplied.get('identity') != identity
            or not re.fullmatch(r'request-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', nonce)):
        raise ValueError('Request preparation correlation binding failed; retain files without sending')
    return supplied


def protocol_header(text, *, occurrences=None):
    """Only the leading authored metadata block, never examples in the body."""
    text = text.lstrip()
    # Rendered markdown normally loses these wrappers; legacy AX can retain them.
    text = re.sub(r'^(?:#{1,6}\s+)?(?:Metadata|메타데이터)\s*[:：]?\s*\n', '', text, flags=re.I)
    text = text.replace('**', '')
    text = re.sub(r'^#{1,6}\s+(?=TASK_ID:)', '', text)
    values = {}
    token = re.compile(r'(TASK_ID|WORKSPACE_ID|ITERATION|PHASE|REQUEST_ID|STATE):\s*([A-Za-z0-9_.:-]+)')
    position = 0
    separator_seen = False
    while True:
        match = token.match(text, position)
        if not match:
            # A single standalone C2C label may separate the complete identity
            # from its immediately following state. Never search the body for
            # a state or accept a label before the four identities are bound.
            identities = ('TASK_ID', 'WORKSPACE_ID', 'ITERATION', 'REQUEST_ID')
            if (not separator_seen and 'STATE' not in values and
                    all(len(values.get(key, set())) == 1 for key in identities)):
                separator = re.match(r'\[C2C\][ \t]*\r?\n\s*(?=STATE:)', text[position:])
                if separator:
                    separator_seen = True
                    position += separator.end()
                    continue
            break
        if occurrences is not None:
            occurrences[match[1]] = occurrences.get(match[1], 0) + 1
        values.setdefault(match[1], set()).add(match[2])
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    return values


def marker_values(text, key):
    return protocol_header(text).get(key, set())


def reply_identity_matches(protocol, message, strict=False):
    for key in ('TASK_ID', 'ITERATION', 'WORKSPACE_ID', 'REQUEST_ID'):
        expected = marker_values(message, key)
        actual = marker_values(protocol, key)
        if len(expected) > 1 or len(actual) > 1:
            return False
        if expected and actual and expected != actual:
            return False
        if strict and expected and not actual:
            return False
    return True


def protocol_response(current):
    segments = current.get('protocolSegments')
    if segments is None:
        segments = [visible_text(current['text'])]
    headers = []
    for segment in segments:
        occurrences = {}
        header = protocol_header(segment, occurrences=occurrences)
        if header:
            headers.append((header, occurrences))
    if len(headers) != 1:
        return ''
    header, occurrences = headers[0]
    if any(count != 1 for count in occurrences.values()) or any(len(values) != 1 for values in header.values()):
        return ''
    return ' '.join(key + ': ' + next(iter(values)) for key, values in header.items())


def associated_observation(current, baseline, message, pinned_user_id=None, generation=None):
    if generation is None or message is None:
        return None
    selected = rendered_request(current, message)
    if (selected and selected.get('associatedDOM') and selected.get('userMessageId')
            and not selected.get('unsupportedPostedStructure')
            and (not pinned_user_id or selected.get('userMessageId') == pinned_user_id)):
        return selected
    return None


def eligible(current, baseline, task, message=None, pinned_user_id=None, request_proof=None):
    check_identity(current, baseline)
    generation = validated_generation(baseline, message, request_proof) if message is not None else None
    if generation is None:
        return False
    if message is not None:
        selected = associated_observation(current, baseline, message, pinned_user_id, generation)
        if selected is None:
            return False
        current = selected
    user = user_body(current)
    reply = visible_text(current['text'])
    protocol = protocol_response(current) if current.get('associatedDOM') else reply
    if current.get('associatedDOM') and not current.get('responseMessageId'):
        return False
    request_matches = associated_observation(current, baseline, message, pinned_user_id, generation) is not None if message is not None else (
        new_user_turn(current, baseline) and has_task(user, task))
    return (request_matches and not current['generating'] and current['complete']
            and response_body(current['text']) != response_body(baseline['text'])
            and has_task(protocol, task)
            and (message is None or reply_identity_matches(protocol, message, current.get('associatedDOM', False)))
            and (len(marker_values(protocol, 'STATE')) == 1 and bool(marker_values(protocol, 'STATE') & {'PLAN', 'REVISE', 'DONE', 'BLOCKED'})
                 if current.get('associatedDOM') else bool(re.search(r'\bSTATE:\s*(?:PLAN|REVISE|DONE|BLOCKED)\b', reply))))


def completed_protocol_identity(selected, message, pinned_user_id):
    """Only complete, pinned, uniquely associated canonical authored headers."""
    if (not selected or not pinned_user_id or not selected.get('associatedDOM')
            or selected.get('userMessageId') != pinned_user_id
            or not selected.get('responseMessageId') or selected.get('generating') is not False
            or selected.get('complete') is not True):
        return None
    expected = protocol_header(message)
    # Count raw authored tokens before set normalization can hide duplicates.
    segments = selected.get('protocolSegments')
    if segments is None:
        segments = [visible_text(selected['text'])]
    headers = []
    for segment in segments:
        occurrences = {}
        header = protocol_header(segment, occurrences=occurrences)
        if header:
            headers.append((header, occurrences))
    keys = ('TASK_ID', 'WORKSPACE_ID', 'ITERATION', 'REQUEST_ID')
    if (len(headers) != 1 or
            any(headers[0][1].get(key, 0) != 1 for key in (*keys, 'STATE'))):
        return None
    actual = headers[0][0]
    if (any(len(expected.get(key, set())) != 1 or len(actual.get(key, set())) != 1 for key in keys)
            or len(actual.get('STATE', set())) != 1):
        return None
    return ({key: next(iter(expected[key])) for key in keys},
            {key: next(iter(actual[key])) for key in keys}, next(iter(actual['STATE'])))


def explicit_reply_mismatch(selected, message, pinned_user_id):
    """An explicit canonical identity conflict is a refusal, regardless of STATE."""
    identity = completed_protocol_identity(selected, message, pinned_user_id)
    if identity is None:
        return None
    wanted, observed, state = identity
    conflicts = {key: {'expected': wanted[key], 'observed': observed[key]}
                 for key in wanted if wanted[key] != observed[key]}
    if not conflicts:
        return None
    return {'expectedIdentity': wanted, 'observedIdentity': observed, 'conflicts': conflicts}


def explicit_reply_invalid_state(selected, message, pinned_user_id):
    """Exact identities and a stable completed invalid review STATE are refused."""
    identity = completed_protocol_identity(selected, message, pinned_user_id)
    if identity is None:
        return None
    wanted, observed, state = identity
    allowed = ('PLAN', 'REVISE', 'DONE', 'BLOCKED')
    if wanted != observed or state in allowed:
        return None
    return {'expectedIdentity': wanted, 'observedIdentity': observed,
            'observedState': state, 'allowedStates': list(allowed)}


def wait_reply(baseline, task, timeout=600, interval=20, read=read_page, sleep=time.sleep, clock=time.monotonic, message=None, protect=None, receipt=None, request_proof=None):
    pinned_user_id = None
    attempt = None
    if receipt is not None:
        attempt = json.loads(receipt.read_text())
        if message is None or attempt.get('page') != baseline['page'] or attempt.get('messageSha256') != hashlib.sha256(message.encode()).hexdigest():
            raise ValueError('Receipt belongs to a different page or request')
        check_identity({**baseline, 'url': attempt.get('url', baseline['url'])}, baseline)
        if attempt.get('posted') is True:
            pinned_user_id = attempt.get('userMessageId')
    generation = validated_generation(baseline, message, request_proof, attempt) if message is not None else None
    if generation is None:
        raise ValueError('Reply admission HOLD: no original qualified request preparation correlation')
    deadline = clock() + timeout
    last = None
    last_mismatch = None
    errors = 0
    identity = {**baseline, 'url': attempt.get('url', baseline['url'])} if receipt is not None else baseline
    while clock() < deadline:
        if protect:
            protect(baseline['page'])
        try:
            current = read(baseline['page'])
            try:
                check_identity(current, identity)
            except ValueError:
                last = None
                last_mismatch = None
                raise  # Preserve the existing fail-closed identity-drift contract.
            if '/c/' not in identity['url'] and '/c/' in current['url']:
                identity = current  # Pin the first acquired conversation for subsequent reads.
            errors = 0
            selected = associated_observation(current, baseline, message, pinned_user_id, generation) if message is not None else None
            stable = {'page': current['page'], 'url': current['url'],
                      'userId': selected.get('userMessageId'), 'replyId': selected.get('responseMessageId'),
                      'user': user_body(selected), 'reply': response_body(selected['text']),
                      'complete': selected['complete']} if selected else None
            digest = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest() if selected and eligible(current, baseline, task, message, pinned_user_id, generation) else None
            if digest is not None and digest == last:
                return selected
            last = digest
            mismatch = explicit_reply_mismatch(selected, message, pinned_user_id)
            refusal = mismatch or explicit_reply_invalid_state(selected, message, pinned_user_id)
            refusal_type = 'reply_mismatch' if mismatch else 'reply_invalid_state'
            negative = {**stable, 'user': user_body(selected), 'reply': selected['text'],
                        'refusalType': refusal_type, 'explicitRefusal': refusal} if refusal else None
            negative_digest = canonical_sha(negative) if negative else None
            if negative_digest is not None and negative_digest == last_mismatch:
                return {'type': refusal_type, 'ok': False, 'complete': False,
                        'page': selected['page'], 'url': selected['url'], 'count': selected['count'],
                        **refusal, 'rejectedResponse': selected,
                        'generationProof': generation, 'generationProofSha256': canonical_sha(generation),
                        'reason': ('Stable completed associated reply has conflicting canonical protocol IDs'
                                   if mismatch else 'Stable completed associated reply has an invalid review STATE'),
                        'stableObservations': 2, 'pollIntervalSeconds': interval}
            last_mismatch = negative_digest
        except TransientEmptyPage:
            last = None  # A blank observation cannot count toward two stable reads.
            last_mismatch = None
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            last = None
            last_mismatch = None
            errors += 1
            if errors >= 3:
                raise RuntimeError('Three snapshot failures; inspect this page without resending.')
        sleep(min(interval, max(0, deadline - clock())))
    raise TimeoutError('Posting or reply remains unconfirmed/pending; reconcile the same page and request. Do not resend or report non-posting from a timeout.')


def normalized(text):
    return ' '.join(text.split())


def posted(current, baseline, message, request_proof=None):
    check_identity(current, baseline)
    return associated_observation(current, baseline, message, generation=validated_generation(baseline, message, request_proof)) is not None


def confirm_posting(baseline, message, receipt, timeout=120, interval=20,
                    read=read_page, sleep=time.sleep, clock=time.monotonic, protect=None, request_proof=None):
    """Observe a prior send only; a timeout is uncertainty, never permission to resend."""
    attempt = json.loads(receipt.read_text())
    digest = hashlib.sha256(message.encode()).hexdigest()
    if attempt.get('page') != baseline['page'] or attempt.get('messageSha256') != digest:
        raise ValueError('Receipt belongs to a different page or request')
    generation = validated_generation(baseline, message, request_proof, attempt)
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
            selected = rendered_request(current, message)
            pinned = (generation is not None and attempt.get('posted') is True and attempt.get('userMessageId')
                      and selected and selected.get('userMessageId') == attempt['userMessageId'])
            confirmed = attempt.get('posted') is True
            attempt['lastObservation'] = ('REQUEST_ID_DRIFT' if confirmed and selected and attempt.get('userMessageId') and selected.get('userMessageId') != attempt['userMessageId']
                                          else 'REQUEST_NOT_RENDERED' if not selected else 'REQUEST_RENDERED')
            if pinned or (not confirmed and posted(current, baseline, message, generation)):
                result = {**attempt, 'posted': True, 'status': 'POSTED',
                          'page': current['page'], 'url': current['url'],
                          'userMessageId': rendered_request(current, message).get('userMessageId'),
                          'nextAction': 'wait_same_baseline'}
                receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
                return result
        except TransientEmptyPage:
            attempt = {**attempt, 'lastObservation': 'TRANSIENT_EMPTY_PAGE'}
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            errors += 1
            if errors >= 3:
                raise RuntimeError('Three snapshot failures; retain the attempt receipt and reconcile without sending.')
        if clock() >= deadline:
            result = {**attempt, 'posted': True if attempt.get('posted') is True else None,
                      'status': 'POSTED' if attempt.get('posted') is True else 'POSTING_UNCONFIRMED',
                      'lastObservation': attempt.get('lastObservation', 'REQUEST_NOT_CONFIRMED_IN_CURRENT_RENDER'),
                      'url': identity['url'], 'replyAdmission': 'QUALIFIED' if generation else 'HOLD_UNQUALIFIED_LEGACY',
                      'nextAction': 'reconcile_same_page_no_resend'}
            receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
            return result
        sleep(min(interval, max(0, deadline - clock())))


class ObservationsExhausted(Exception):
    """Replay consumed every supplied observation; observe again, never resend."""


def load_observation(path):
    """The saved tool result may be the JSON object or its JSON string."""
    value = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or value.get('schema') != OBSERVATION_SCHEMA:
        raise ValueError('Not a %s observation: %s' % (OBSERVATION_SCHEMA, path))
    if not isinstance(value.get('url'), str) or type(value.get('observedAt')) not in (int, float):
        raise ValueError('Observation lacks url/observedAt: %s' % path)
    return value


def page_from_observation(observation, page):
    """The read_page shape from one DOM observation, without AX snapshot fields."""
    posted = observation.get('posted') or {}
    turns = posted.get('turns') or []
    if not isinstance(turns, list) or not all(
            isinstance(t, dict) and t.get('role') in ('user', 'assistant') and isinstance(t.get('body'), str)
            for t in turns):
        raise ValueError('Observation turns are malformed; observe again without sending')
    users = [t for t in turns if t['role'] == 'user']
    assistants = [t for t in turns if t['role'] == 'assistant']
    latest = turns[-1] if turns and turns[-1]['role'] == 'assistant' else None
    user_body, user_id = posted.get('body'), posted.get('id')
    if user_body is None and users:
        user_body, user_id = users[-1]['body'], users[-1].get('id')
    boxes = observation.get('composer') or []
    refs = ({'composer': {'role': 'textbox', 'name': boxes[0].get('accessibleName', '')}}
            if len(boxes) == 1 else {})
    return {'page': page, 'url': observation['url'], 'count': len(assistants),
            'text': latest['body'] if latest else '', 'userCount': len(users),
            'lastUser': users[-1]['body'] if users else '', 'refs': refs,
            'userBody': user_body, 'userMessageId': user_id, 'postedTurns': turns,
            'emptyDocument': bool(observation.get('emptyDocument')),
            'generating': observation.get('generating') is True,
            'complete': bool(latest and latest.get('complete') is True)}


class ObservationReplay:
    """Feed saved observations to the polling helpers; clock follows observedAt."""

    def __init__(self, paths, page, min_gap=0):
        if page is None or not page.startswith(AGENT_PAGE_PREFIX):
            raise ValueError('Agent-driven baselines use an agent: page identity')
        self.observations = [load_observation(p) for p in paths]
        if not self.observations:
            raise ValueError('Supply at least one observation')
        times = [o['observedAt'] / 1000 for o in self.observations]
        for before, after in zip(times, times[1:]):
            # A repeated or rushed observation must not count as a second stable read.
            if after - before < max(min_gap, 0.001):
                raise ValueError('Observations must be distinct and at least %ss apart, in order' % min_gap)
        self.page, self.times, self.index = page, times, 0
        self.now = times[0]

    def read(self, page):
        if page != self.page:
            raise ValueError('Browser page identity mismatch')
        if self.index >= len(self.observations):
            raise ObservationsExhausted()
        observation = self.observations[self.index]
        self.now = self.times[self.index]
        self.index += 1
        return page_from_observation(observation, page)

    def clock(self):
        return self.now

    @property
    def elapsed(self):
        return self.times[-1] - self.times[0]


def agent_capture(observation, page):
    if not page.startswith(AGENT_PAGE_PREFIX):
        raise ValueError('Agent-driven page identity must start with ' + AGENT_PAGE_PREFIX)
    current = page_from_observation(observation, page)
    check_identity(current, current)
    if current['generating']:
        raise ValueError('Page is generating; observe existing request before capturing a new baseline.')
    boxes = observation.get('composer') or []
    if len(boxes) != 1:
        raise ValueError('Expected one composer on the owned page')
    return {**current, 'observedAt': observation['observedAt'] / 1000,
            'draftAtCapture': boxes[0].get('text', ''),
            'mentionsAtCapture': boxes[0].get('mentions', []),
            'unknownDraft': bool(normalized(boxes[0].get('text', '')))}


def agent_check_send(baseline, message, receipt, observation, request_proof=None):
    """Every pre-click check of submit; the agent clicks only the returned selector."""
    if receipt.exists():
        raise ValueError('Receipt exists: never click again; run agent-confirm on the same receipt')
    generation = validated_generation(baseline, message, request_proof)
    if generation is None:
        raise ValueError('Submit HOLD: prepare a new request with its original correlation proof; not sent')
    if observation['observedAt'] / 1000 <= baseline.get('observedAt', 0):
        raise ValueError('Observation predates the baseline; observe again')
    current = page_from_observation(observation, baseline['page'])
    check_identity(current, baseline)
    digest = hashlib.sha256(message.encode()).hexdigest()
    if rendered_request(current, message):
        result = {'posted': None, 'status': 'POSTING_UNCONFIRMED', 'alreadyPresent': True,
                  'attempted': False, 'page': baseline['page'], 'url': current['url'],
                  'messageSha256': digest, 'nextAction': 'reconcile_same_page_no_resend',
                  'generationProof': generation, 'generationProofSha256': canonical_sha(generation)}
        receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
        return result
    if new_user_turn(current, baseline) or current['generating']:
        raise ValueError('Page changed since capture; reconcile before sending')
    boxes = observation.get('composer') or []
    if len(boxes) != 1:
        raise ValueError('Expected one composer on the owned page')
    if normalized(boxes[0].get('text', '')) != normalized(message):
        raise ValueError('Composer does not hold the exact prepared message; correct it and observe again; not sent')
    chips = boxes[0].get('mentions', [])
    state = observation.get('sendReady') or {}
    selector = state.get('selector')
    if state.get('ready') is not True or selector not in [
            'form button[aria-label=' + json.dumps(name, ensure_ascii=False) + ']'
            for name in ('보내기', 'Send', 'Send message')]:
        raise ValueError('Send button not ready (%s); draft retained, no click' % state.get('reason', 'unknown'))
    receipt.write_text(json.dumps({'attempted': True, 'status': 'ATTEMPTED', 'mode': 'agent',
                                   'page': baseline['page'], 'url': current['url'], 'messageSha256': digest,
                                   'mentions': chips, 'selector': selector,
                                   'generationProof': generation, 'generationProofSha256': canonical_sha(generation)},
                                  ensure_ascii=False))
    return {'ok': True, 'status': 'READY_TO_CLICK', 'selector': selector, 'mentions': chips,
            'nextAction': 'click_selector_once_then_agent_confirm'}


def agent_pending(replay, timeout, interval=20):
    if replay.elapsed >= timeout:
        raise TimeoutError('Posting or reply remains unconfirmed/pending; reconcile the same page and request. Do not resend or report non-posting from a timeout.')
    return {'ok': False, 'status': 'PENDING', 'observations': len(replay.observations),
            'nextPollSeconds': interval,
            'nextAction': 'observe_again_and_rerun_with_all_observations_no_resend'}


def agent_confirm(baseline, message, receipt, paths, timeout=120, request_proof=None):
    replay = ObservationReplay(paths, baseline['page'])
    try:
        return confirm_posting(baseline, message, receipt, timeout=timeout, read=replay.read,
                               sleep=lambda seconds: None, clock=replay.clock, request_proof=request_proof)
    except ObservationsExhausted:
        return agent_pending(replay, float('inf'))


def agent_wait(baseline, task, message, receipt, paths, timeout=600, request_proof=None):
    replay = ObservationReplay(paths, baseline['page'], AGENT_MIN_GAP_SECONDS)
    try:
        return wait_reply(baseline, task, timeout, read=replay.read, sleep=lambda seconds: None,
                          clock=replay.clock, message=message, receipt=receipt, request_proof=request_proof)
    except ObservationsExhausted:
        return agent_pending(replay, timeout)


def composer(page):
    result = orca(page, 'eval', '--expression', COMPOSER_EXPRESSION)
    values = json.loads(result['result'])
    if len(values) != 1:
        raise ValueError('Expected one composer on the owned page')
    return ComposerText(values[0]['text'], values[0].get('mentions', []), values[0].get('accessibleName'))


def owned_draft_matches(draft, message, known_draft):
    """Exact editor serialization: one trailing separator only for one chip."""
    chips = list(getattr(draft, 'mentions', []))
    same_bytes = known_draft is not None and (str(draft) == known_draft or
        (len(chips) == 1 and str(draft) == known_draft + ' '))
    before, after = protocol_header(str(draft)).get('TASK_ID', set()), protocol_header(message).get('TASK_ID', set())
    return same_bytes and len(before) == 1 and before == after


def native_focus_editor(page, box):
    """Fresh editable viewport hit; no blind coordinates or control target."""
    hit_expression = """JSON.stringify((()=>{
      const boxes=Array.from(document.querySelectorAll('form [role=textbox][contenteditable=true]'));
      if(boxes.length!==1)return {ok:false};
      const e=boxes[0],r=e.getBoundingClientRect();
      const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
      if(right<=left||bottom<=top)return {ok:false};
      for(const fy of [0.8,0.5,0.2])for(const fx of [0.1,0.5,0.9]){
        const x=left+(right-left)*fx,y=top+(bottom-top)*fy,t=document.elementFromPoint(x,y);
        if(t&&(t===e||e.contains(t))&&t.isContentEditable&&!t.closest('[contenteditable=false],a,button'))return {ok:true,x,y};
      }
      return {ok:false};
    })())"""
    hit = json.loads(orca(page, 'eval', '--expression', hit_expression)['result'])
    if not hit.get('ok'):
        raise ValueError('Owned editable hit target unavailable; not sent')
    orca(page, 'mouse', 'move', '--x', str(hit['x']), '--y', str(hit['y']))
    orca(page, 'mouse', 'down', '--button', 'left')
    orca(page, 'mouse', 'up', '--button', 'left')
    orca(page, 'focus', '--element', '@' + box)


def insert_native_lines(page, message):
    """Preserve original LF separators through native input, never flatten."""
    for index, line in enumerate(message.split('\n')):
        if index:
            orca(page, 'keypress', '--key', 'Shift+Enter')
        if line:
            orca(page, 'inserttext', '--text', line)


def insert_empty_chip_draft(baseline, message, chips, box):
    """The empty editor is rechecked around native focus before insertion."""
    page = baseline['page']
    current = read_page(page)
    check_identity(current, baseline)
    draft = composer(page)
    if normalized(draft) or list(draft.mentions) != chips:
        raise ProtectedDraft('Empty composer or connector changed before focus; retained', draft)
    native_focus_editor(page, box)
    current = read_page(page)
    check_identity(current, baseline)
    draft = composer(page)
    if normalized(draft) or list(draft.mentions) != chips:
        raise ProtectedDraft('Empty composer or connector changed after focus; retained', draft)
    if orca(page, 'eval', '--expression', CARET_START_EXPRESSION).get('result') != 'caret-start':
        raise ValueError('Composer caret unavailable; not sent')
    insert_native_lines(page, message)


def replace_owned_chip_draft(baseline, message, known_draft, chips, box):
    """Native text replacement; never delete nodes, chip, or dispatch Send."""
    page = baseline['page']
    current = read_page(page)
    check_identity(current, baseline)
    draft = composer(page)
    if not owned_draft_matches(draft, message, known_draft) or list(draft.mentions) != chips:
        raise ProtectedDraft('Owned draft changed before native focus; retained', draft)
    native_focus_editor(page, box)
    current = read_page(page)
    check_identity(current, baseline)
    draft = composer(page)
    if not owned_draft_matches(draft, message, known_draft) or list(draft.mentions) != chips:
        raise ProtectedDraft('Owned draft changed after native focus; retained', draft)
    selection_expression = """JSON.stringify((()=>{
      %s
      const boxes=Array.from(document.querySelectorAll('form [role=textbox][contenteditable=true]'));
      if(boxes.length!==1)return {ok:false,reason:'composer-count'};
      const e=boxes[0], chips=Array.from(e.querySelectorAll(%s)),read=withoutMentions(e);
      const expected=%s,names=%s;
      if(chips.length!==1||JSON.stringify(read.mentions)!==JSON.stringify(names))return {ok:false,reason:'chip-identity'};
      if(read.text!==expected&&read.text!==expected+' ')return {ok:false,reason:'owned-text-mismatch'};
      if(chips[0].getAttribute('contenteditable')!=='false')return {ok:false,reason:'chip-editability'};
      const r=document.createRange();r.setStart(e,0);r.setEndBefore(chips[0]);
      if(r.cloneContents().querySelector('[contenteditable=false]')||r.toString()!==expected)return {ok:false,reason:'unsafe-range'};
      const selected=getSelection();selected.removeAllRanges();selected.addRange(r);
      if(selected.rangeCount!==1||selected.getRangeAt(0).toString()!==expected)return {ok:false,reason:'selection-drift'};
      return {ok:true,reason:'owned-text-selected'};
    })())""" % (WITHOUT_MENTIONS, json.dumps(MENTION), json.dumps(known_draft), json.dumps(chips))
    selection = json.loads(orca(page, 'eval', '--expression', selection_expression)['result'])
    if not selection.get('ok'):
        raise ValueError('Owned text selection unavailable: ' + selection.get('reason', 'unknown') + '; not sent')
    insert_native_lines(page, message)


def wait_send_ready(baseline, identity, message, chips, timeout=15, interval=0.5,
                    read=None, clock=time.monotonic, sleep=time.sleep):
    """Wait before the first attempt only; preserve the exact draft on failure."""
    deadline = clock() + timeout
    read = read or read_page
    captured_layout = False
    while True:
        current = read(baseline['page'])
        check_identity(current, identity)
        if rendered_request(current, message) or new_user_turn(current, baseline) or current['generating']:
            raise ValueError('Posted request appeared or page changed while preparing send; not sent')
        draft = composer(baseline['page'])
        if normalized(draft) != normalized(message) or list(getattr(draft, 'mentions', [])) != chips:
            raise ValueError('Composer or connector changed while preparing send; not sent')
        state = json.loads(orca(baseline['page'], 'eval', '--expression', SEND_READY_EXPRESSION)['result'])
        if state.get('ready') is True:
            selector = state.get('selector')
            if selector not in ['form button[aria-label=' + json.dumps(name, ensure_ascii=False) + ']'
                                for name in ('보내기', 'Send', 'Send message')]:
                raise ValueError('Unexpected send selector; not sent')
            return current, selector
        if clock() >= deadline:
            raise ValueError('Send button not ready (%s); draft retained, no click attempted' % state.get('reason', 'unknown'))
        if state.get('reason') == 'send-obscured' and not captured_layout:
            # Background WebContents can retain pre-animation geometry. Orca's
            # native capture paints the owned page; discard the image and
            # recheck the draft, conversation and hit target before any click.
            orca(baseline['page'], 'screenshot')
            captured_layout = True
        sleep(min(interval, max(0, deadline - clock())))


def submit(baseline, message, receipt, replace_owned_draft=False, known_draft=None, posting_timeout=120, protect=None, request_proof=None):
    page = baseline['page']
    if receipt.exists():
        return confirm_posting(baseline, message, receipt, timeout=posting_timeout, protect=protect, request_proof=request_proof)
    generation = validated_generation(baseline, message, request_proof)
    current = read_page(page)
    if posted(current, baseline, message, generation):
        if receipt.exists():
            return confirm_posting(baseline, message, receipt, timeout=0)
        result = {'posted': True, 'status': 'POSTED', 'alreadyPosted': True, 'attempted': False,
                  'page': page, 'url': current['url'], 'nextAction': 'wait_same_baseline',
                  'messageSha256': hashlib.sha256(message.encode()).hexdigest(),
                  'userMessageId': rendered_request(current, message).get('userMessageId'),
                  'generationProof': generation, 'generationProofSha256': canonical_sha(generation)}
        receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
        return result
    if receipt.exists():
        return confirm_posting(baseline, message, receipt, timeout=posting_timeout, protect=protect, request_proof=request_proof)
    if rendered_request(current, message):
        result = {'posted': None, 'status': 'POSTING_UNCONFIRMED', 'alreadyPresent': True,
                  'attempted': False, 'page': page, 'url': current['url'],
                  'messageSha256': hashlib.sha256(message.encode()).hexdigest(),
                  'nextAction': 'reconcile_same_page_no_resend'}
        if generation is not None:
            result.update(generationProof=generation, generationProofSha256=canonical_sha(generation))
        receipt.write_text(json.dumps(result, ensure_ascii=False) + '\n')
        return result
    if generation is None:
        raise ValueError('Submit HOLD: prepare a new request with its original correlation proof; not sent')
    if new_user_turn(current, baseline) or current['generating']:
        raise ValueError('Page changed since capture; reconcile before sending')
    check_identity(current, baseline)  # Before any editor mutation or focus.
    identity = current if '/c/' in current['url'] else baseline
    draft = composer(page)
    if normalized(draft) and normalized(draft) != normalized(message):
        if not replace_owned_draft or not owned_draft_matches(draft, message, known_draft):
            raise ProtectedDraft('Different draft protected: replacement requires a matching previously saved draft file with the same TASK_ID. Tab inactivity is not ownership.', draft)
    boxes = [ref for ref, meta in current['refs'].items() if meta.get('role') == 'textbox']
    # Every AX ref must match the unique live form editor, including a single ref.
    name = getattr(draft, 'accessible_name', None)
    boxes = [ref for ref in boxes if isinstance(name, str) and name and current['refs'][ref].get('name') == name]
    if len(boxes) != 1:
        raise ValueError('Expected one accessible composer')
    chips = list(getattr(draft, 'mentions', []))
    prepared = normalized(draft) == normalized(message)
    owned_chip_replacement = bool(chips and normalized(draft) and not prepared)
    if protect and not prepared:
        guard_input(page, enabled=False)  # Only after the draft check; readback catches stray keys.
    if prepared:
        pass  # Preserve the already verified request; refilling restarts layout animations.
    elif owned_chip_replacement:
        replace_owned_chip_draft(baseline, message, known_draft, chips, boxes[0])
    elif chips:
        insert_empty_chip_draft(baseline, message, chips, boxes[0])
    else:
        orca(page, 'fill', '--element', '@' + boxes[0], '--value', message)
    written = composer(page)
    if normalized(written) != normalized(message):
        raise ValueError('Composer readback differs from message file; not sent')
    if list(getattr(written, 'mentions', [])) != chips:
        raise ValueError('Connector chip changed while composing; not sent')
    current, selector = wait_send_ready(baseline, identity, message, chips)
    # Recheck one coherent readiness observation immediately before the receipt.
    # Browser calls are sequential; this is not an atomic DOM/send transaction.
    current, selector = wait_send_ready(baseline, identity, message, chips)
    receipt.write_text(json.dumps({'attempted': True, 'status': 'ATTEMPTED', 'page': page,
                                  'url': current['url'], 'messageSha256': hashlib.sha256(message.encode()).hexdigest(),
                                  'generationProof': generation, 'generationProofSha256': canonical_sha(generation)}))
    orca(page, 'click', '--element', selector)
    return confirm_posting(baseline, message, receipt, timeout=posting_timeout, protect=protect, request_proof=request_proof)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    opener = sub.add_parser('open', help='Create an owned tab and block stray keyboard input into it')
    opener.add_argument('--url', required=True)
    opener.add_argument('--worktree', help='Orca browser worktree (Orca backend only)')
    opener.add_argument('--backend', choices=('orca', 'cdp'), default='orca',
                        help='cdp: a Chrome you launched with --remote-debugging-port on 127.0.0.1')
    opener.add_argument('--output', type=Path, required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('--page', required=True)
    capture.add_argument('--output', type=Path, required=True)
    wait = sub.add_parser('wait')
    wait.add_argument('--baseline', type=Path, required=True)
    wait.add_argument('--task', required=True)
    wait.add_argument('--output', type=Path, required=True)
    wait.add_argument('--message-file', type=Path)
    wait.add_argument('--receipt', type=Path, help='Receipt with original validated preparation proof may pin its confirmed user identity')
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
    for parser in (wait, send, reconcile):
        parser.add_argument('--request-proof', type=Path)
    sub.add_parser('observe-expression', help='Print the read-only JS an agent runs with its own browser tool')
    a_capture = sub.add_parser('agent-capture', help='Baseline from one saved agent observation')
    a_capture.add_argument('--observation', type=Path, required=True)
    a_capture.add_argument('--page-id', required=True, help='agent:<stable tab label>')
    a_capture.add_argument('--output', type=Path, required=True)
    a_send = sub.add_parser('agent-check-send', help='Pre-click checks; writes the attempt receipt and returns the selector')
    a_send.add_argument('--observation', type=Path, required=True)
    a_confirm = sub.add_parser('agent-confirm', help='Replay observations after the single click')
    a_confirm.add_argument('--timeout', type=int, default=120, choices=range(0, 601))
    a_wait = sub.add_parser('agent-wait', help='Replay paced observations until a stable reply')
    a_wait.add_argument('--task', required=True)
    a_wait.add_argument('--output', type=Path, required=True)
    a_wait.add_argument('--timeout', type=int, default=600, choices=range(20, 1801))
    for parser in (a_send, a_confirm, a_wait):
        parser.add_argument('--baseline', type=Path, required=True)
        parser.add_argument('--message-file', type=Path, required=True)
        parser.add_argument('--receipt', type=Path, required=True)
        parser.add_argument('--request-proof', type=Path)
    for parser in (a_confirm, a_wait):
        parser.add_argument('--observation', type=Path, nargs='+', required=True,
                            help='Every observation since the click, oldest first')
    args = p.parse_args()
    baseline = None
    if args.action == 'observe-expression':
        print(OBSERVE_EXPRESSION)
        return 0
    if args.action.startswith('agent-'):
        return agent_main(args)
    try:
        if args.action == 'open':
            if args.backend == 'orca' and not args.worktree:
                raise ValueError('--worktree is required for the Orca backend')
            result = open_tab(args.url, args.worktree, backend=args.backend)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'ok': not result['unknownDraft'], **result}, ensure_ascii=False))
            return 0 if not result['unknownDraft'] else 2
        if args.action in ('submit', 'reconcile'):
            baseline, message = json.loads(args.baseline.read_text()), args.message_file.read_text()
            if args.action == 'submit':
                result = submit(baseline, message, args.receipt, args.replace_owned_draft, args.owned_draft_file.read_text() if args.owned_draft_file else None, args.posting_timeout, protect=keep_guarded, request_proof=args.request_proof)
            else:
                result = confirm_posting(baseline, message, args.receipt, timeout=args.timeout, protect=keep_guarded, request_proof=args.request_proof)
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
                                protect=keep_guarded, receipt=args.receipt, request_proof=args.request_proof)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        if result.get('type') in ('reply_mismatch', 'reply_invalid_state'):
            print(json.dumps({'ok': False, 'type': result['type'], 'complete': False,
                              'output': str(args.output), 'page': result['page'], 'url': result['url'],
                              'expectedIdentity': result['expectedIdentity'],
                              'observedIdentity': result['observedIdentity'],
                              **({'observedState': result['observedState']}
                                 if 'observedState' in result else {})}))
            return 2
        print(json.dumps({'ok': True, 'output': str(args.output), 'page': result['page'], 'url': result['url'], 'count': result['count']}))
        return 0
    except ProtectedDraft as error:
        print(json.dumps({'ok': False, 'error': str(error), 'protectedDraft': error.draft}, ensure_ascii=False))
        return 2
    except (ValueError, RuntimeError, TimeoutError, OSError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2
    finally:
        if args.action == 'submit' and baseline is not None:
            keep_guarded(baseline['page'])


def agent_main(args):
    """Agent-driven delivery: the agent observes and clicks; this only judges."""
    try:
        if args.action == 'agent-capture':
            result = agent_capture(load_observation(args.observation), args.page_id)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'ok': not result['unknownDraft'], 'output': str(args.output),
                              'page': result['page'], 'url': result['url'],
                              'unknownDraft': result['unknownDraft'],
                              **({'draftAtCapture': result['draftAtCapture']} if result['unknownDraft'] else {})},
                             ensure_ascii=False))
            return 0 if not result['unknownDraft'] else 2
        baseline, message = json.loads(args.baseline.read_text()), args.message_file.read_text()
        if args.action == 'agent-check-send':
            result = agent_check_send(baseline, message, args.receipt, load_observation(args.observation),
                                      args.request_proof)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('status') == 'READY_TO_CLICK' else 2
        if args.action == 'agent-confirm':
            result = agent_confirm(baseline, message, args.receipt, args.observation, args.timeout,
                                   args.request_proof)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('posted') is True else 3 if result.get('status') == 'PENDING' else 2
        receipt = args.receipt if args.receipt.exists() else None
        result = agent_wait(baseline, args.task, message, receipt, args.observation, args.timeout,
                            args.request_proof)
        if result.get('status') == 'PENDING':
            print(json.dumps(result))
            return 3
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        if result.get('type') in ('reply_mismatch', 'reply_invalid_state'):
            print(json.dumps({'ok': False, 'type': result['type'], 'complete': False, 'output': str(args.output),
                              'expectedIdentity': result['expectedIdentity'],
                              'observedIdentity': result['observedIdentity'],
                              **({'observedState': result['observedState']} if 'observedState' in result else {})}))
            return 2
        print(json.dumps({'ok': True, 'output': str(args.output), 'page': result['page'],
                          'url': result['url'], 'count': result['count']}))
        return 0
    except (ValueError, RuntimeError, TimeoutError, OSError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
