"""Local preview/authorization ledger and guarded, ordinary page-based messaging."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from .local import ACTIVITY_WAITING, OUTREACH_ATTEMPTED, Stopped, activity_gate, digest, dumps, now, read_json, write_json
from .page import detail_state, page_problem


TERMINAL = OUTREACH_ATTEMPTED
DELIVERY_TIMEOUT = 45


class ChatTimeout(asyncio.TimeoutError):
    def __init__(self, view):
        self.view = view


class Outreach:
    def __init__(self, ws, store):
        self.ws, self.store = ws, store
        self.root = ws.runtime / "outreach"

    def record(self, job_id):
        row = self.store.conn.execute("SELECT * FROM outreach WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def activity(self, job_id):
        return activity_gate(self.store.job(job_id)["recruiter_activity"], self.ws.outreach_activity())

    def ready(self, job_id, review_id=None):
        hold = self.store.activity_hold(job_id)
        if hold and hold["state"] in ACTIVITY_WAITING:
            raise Stopped("saved_hold_requires_explicit_reset")
        review = next((r for r in self.store.reviews() if r["job_id"] == job_id), None)
        if (not review or review["payload"].get("decision") != "shortlist"
                or review["payload"].get("stage") != "detail"
                or not review["payload"].get("greeting_draft")
                or review_id is not None and review["id"] != review_id):
            raise Stopped("outreach_requires_current_shortlisted_draft")
        return review

    def policy(self):
        auth = read_json(self.root / "authorization.json", {})
        valid = (auth.get("schema") == 2 and auth.get("workspace") == str(self.ws.root)
                 and auth.get("scope") in ("batch", "queue") and isinstance(auth.get("job_ids"), list)
                 and bool(auth.get("user_message")) and self.ws.search_plan_status()["state"] == "confirmed")
        return {"mode": "live" if valid else "preview", "authorization": auth if valid else None}

    def preview(self, ids, partial_reason=None, out=None):
        from .reports import create_preview
        return create_preview(self, ids, partial_reason, out)

    def authorize(self, preview_id, user_message, scope="batch"):
        if not user_message.strip():
            raise Stopped("user_send_authorization_required")
        if scope not in ("batch", "queue"):
            raise Stopped("invalid_outreach_authorization_scope")
        preview_id = uuid.UUID(hex=preview_id).hex
        preview = read_json(self.root / "previews" / (preview_id + ".json"))
        if not preview or preview.get("workspace") != str(self.ws.root):
            raise Stopped("outreach_preview_required")
        self.ws.require_search_plan()
        from .reports import snapshot
        current = snapshot(self, [i["job_id"] for i in preview["items"]], preview.get("partial_reason"))
        if not preview["ready_for_confirmation"] or not current["ready_for_confirmation"]:
            raise Stopped("calibration_incomplete:review_ten_and_find_a_contactable_job")
        if current["signature"] != preview["signature"]:
            raise Stopped("outreach_preview_changed")
        initial = [{"job_id": item["job_id"], "review_id": item["review_id"],
                    "facts_hash": item["facts_hash"], "message_hash": digest(item["greeting_draft"])}
                   for item in current["items"] if item["result"] == "send"]
        auth = {"schema": 2, "id": uuid.uuid4().hex, "workspace": str(self.ws.root),
                "user_message": user_message.strip(), "confirmed_at": now(), "preview_id": preview_id,
                "initial_jobs": initial, "scope": scope,
                "job_ids": ([r[0] for r in self.store.conn.execute("SELECT id FROM jobs ORDER BY id")]
                            if scope == "queue" else [i["job_id"] for i in initial])}
        for item in initial:
            self.save(item["job_id"], self.ready(item["job_id"], item["review_id"]),
                      "previewed", {"preview_id": preview_id})
        write_json(self.root / "authorization.json", auth)
        return {"mode": "live", "authorization_id": auth["id"],
                "initial_jobs": [i["job_id"] for i in initial], "scope": scope,
                "queue_jobs": len(auth["job_ids"]), "external_actions": 0}

    def require_authorization(self, job_id, review_id=None, authorization_id=None):
        auth = self.policy()["authorization"]
        if not auth or authorization_id is not None and auth["id"] != authorization_id:
            raise Stopped("outreach_send_authorization_required")
        if job_id not in auth["job_ids"]:
            raise Stopped("outreach_outside_authorized_queue")
        if auth["scope"] == "batch":
            approved = next((i for i in auth["initial_jobs"] if i["job_id"] == job_id), None)
            current = self.ready(job_id, review_id)
            if (not approved or digest(current["payload"]["greeting_draft"]) != approved["message_hash"]):
                raise Stopped("outreach_preview_changed")
        return auth

    def save(self, job_id, review, status, result=None, recipient=None):
        existing = self.record(job_id)
        if existing and existing["status"] in TERMINAL:
            raise Stopped("outreach_already_attempted:" + existing["status"])
        message = review["payload"]["greeting_draft"]
        with self.store.conn:
            self.store.conn.execute("""INSERT INTO outreach
                (job_id,review_id,status,message,message_hash,recipient,created_at,updated_at,result)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                review_id=excluded.review_id,status=excluded.status,message=excluded.message,
                message_hash=excluded.message_hash,recipient=COALESCE(excluded.recipient,outreach.recipient),
                updated_at=excluded.updated_at,result=excluded.result""",
                (job_id, review["id"], status, message, digest(message), recipient, now(), now(), dumps(result or {})))

    def reserve(self, job_id, review, recipient):
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.record(job_id)
            if current and current["status"] in TERMINAL:
                raise Stopped("outreach_already_attempted:" + current["status"])
            if conn.execute("SELECT 1 FROM outreach WHERE recipient=? AND job_id<>? AND status IN ('sending','sent','uncertain','already_contacted','contacted_external')",
                            (recipient, job_id)).fetchone():
                raise Stopped("recipient_already_contacted")
            self.save(job_id, review, "sending", {"phase": "before_contact_click"}, recipient)
        except BaseException:
            conn.rollback()
            raise

    def finish(self, job_id, status, result):
        with self.store.conn:
            self.store.conn.execute("UPDATE outreach SET status=?,updated_at=?,result=? WHERE job_id=?",
                                    (status, now(), dumps(result), job_id))
        return {"job_id": job_id, "status": status, **result}

    def queue(self):
        reviews = {r["job_id"]: r for r in self.store.reviews()}
        groups = {k: [] for k in ("unreviewed", "draft_ready", "reviewed", "read_failed", "contacted", "uncertain")}
        for row in self.store.conn.execute("SELECT id FROM jobs ORDER BY first_seen,id"):
            job_id = row[0]
            job, record, review = self.store.job(job_id), self.record(job_id), reviews.get(job_id)
            if record and record["status"] in TERMINAL:
                group = "uncertain" if record["status"] in ("sending", "uncertain") else "contacted"
            elif (job["activity_hold"] and job["activity_hold"]["state"] in ACTIVITY_WAITING):
                group = "reviewed"
            elif record and record["status"] == "read_failed":
                group = "read_failed"
            elif review and self.store.review_complete(review):
                data = review["payload"]
                group = "draft_ready" if data.get("decision") == "shortlist" and data.get("greeting_draft") else "reviewed"
            elif job["detail_state"] in ("failed", "partial", "unavailable"):
                group = "read_failed"
            else:
                group = "unreviewed"
            groups[group].append(job_id)
        return {"mode": self.policy()["mode"], "counts": {k: len(v) for k, v in groups.items()}, "queues": groups}


# DOM-only adapter: never replay website APIs or store redirect security tokens.
# Both the detail modal and full chat page are ordinary contact destinations.
DOM = r"""(args => {
  const visible = e => e && e.getClientRects().length > 0;
  const all = (sel,root=document) => root ? [...root.querySelectorAll(sel)].filter(visible) : [];
  const normalize = s => (s || '').replace(/\r\n/g,'\n').replace(/\u00a0/g,' ').trim();
  const current = new URL(location.href);
  const detailJob = current.pathname.match(/^\/job_detail\/([^/]+)\.html$/)?.[1] || '';
  const starts = all('.btn-startchat');
  const start = starts.length === 1 ? starts[0] : null;
  const redirect = start?.getAttribute('redirect-url');
  const target = redirect ? new URL(redirect, location.origin) : null;
  const editors = all('#chat-input[contenteditable],textarea.input-area');
  const editor = editors.length === 1 ? editors[0] : null;
  const dialogs = all('.startchat-dialog');
  const dialog = dialogs.length === 1 ? dialogs[0] : null;
  const conversations=all('.chat-conversation'), conversation=conversations.length===1 ? conversations[0] : null;
  const selected=all('.friend-content.selected'), friend=selected.length===1 ? selected[0] : null;
  const identityText=(root,sel)=>{const items=all(sel,root);return items.length===1?normalize(items[0].innerText):'';};
  const identity={recruiter:identityText(conversation,'.base-info .name-text'),
    company:identityText(conversation,'.base-info > span:not(.base-title)'),
    title:identityText(conversation,'.position-content .position-name'),
    selected_recruiter:identityText(friend,'.name-text'),
    selected_company:identityText(friend,'.name-box > .name-text + span')};
  const messageRoot=dialog || (current.pathname==='/web/geek/chat' ? conversation : document);
  const bubbles = all('.item-myself .text,.item-myself .message-text,.message-item.is-self .message-text',messageRoot);
  const messages = bubbles.map(e => {
    const row=e.closest('.item-myself,.message-item');
    const body=e.querySelector('.text-content');
    const copy=e.cloneNode(true);
    copy.querySelectorAll('.message-status').forEach(x=>x.remove());
    const status=row?.querySelector('.message-status'), label=normalize(status?.innerText);
    return {text:normalize(body ? body.innerText : status && e.contains(status) ? copy.textContent : e.innerText),status:label,
      failed:!!e.closest('.message-failed,.send-failed,.is-error') || !!row?.querySelector('.message-failed,.send-failed,.is-error') || /失败|未发送/.test(label),
      pending:!!e.closest('.sending,.is-sending') || !!row?.querySelector('.sending,.is-sending,.status-loading,.status-sending') ||
        !!(label && !/^(已发送|送达|已送达|已读|对方已读)$/.test(label)),
      id:row?.getAttribute('data-mid') || row?.getAttribute('data-id') || row?.id || '', source:'self_message'};
  });
  // Observed detail-page modal: no is-self class; outgoing delivery has its own status.
  // Scope to the visible modal and require an explicit success label, not arbitrary .text.
  const modalRows = dialog ? [...dialog.querySelectorAll('.message-list > .message-item')].filter(visible) : [];
  for (const row of modalRows) {
    if (row.matches('.item-myself,.is-self')) continue;
    const text=row.querySelector('p.text'), status=row.querySelector('.status');
    if (!visible(text) || !visible(status)) continue;
    const label=normalize(status.innerText);
    const failed=/失败|未发送/.test(label) || status.matches('.failed,.error,.fail');
    const confirmed=status.classList.contains('success') && label==='已发送';
    messages.push({text:normalize(text.innerText),failed,pending:!failed && !confirmed,
      id:row.getAttribute('data-id') || row.id || '',status:label,source:'startchat_modal'});
  }
  const view = {url:location.origin+location.pathname,job_id:detailJob || current.searchParams.get('jobId') || '',
    recipient: detailJob ? target?.searchParams.get('id') || '' : current.searchParams.get('id') || '',
    contact_job:target?.searchParams.get('jobId') || '',start_text:normalize(start?.innerText),
    editor:!!editor,editor_text:normalize(editor?.value ?? editor?.innerText),messages,
    surface:dialog ? 'startchat_modal' : 'chat_page',modal_rows:modalRows.length,identity,
    conversation_count:conversations.length,selected_count:selected.length,
    limited:/今日.*(?:沟通|打招呼).*(?:上限|用完)|沟通次数已达上限/.test(document.body?.innerText || '')};
  if (args.op === 'read') return JSON.stringify(view);
  const expected=args.expected_identity || {};
  const visibleMatch=!!conversation && !!friend && ['company','recruiter','title'].every(k=>
    normalize(expected[k]) && identity[k]===normalize(expected[k])) &&
    identity.selected_recruiter===normalize(expected.recruiter) && identity.selected_company===normalize(expected.company);
  const idMatch=view.job_id===args.job_id && view.recipient===args.recipient;
  // This fallback is bound to the already-authorized detail contact, never a search for a new recipient.
  const fullChatMatch=current.pathname==='/web/geek/chat' && !view.job_id && !view.recipient && visibleMatch;
  if (location.hostname !== 'www.zhipin.com' || view.limited ||
      !(idMatch || args.op!=='contact' && fullChatMatch) ||
      (current.pathname==='/web/geek/chat' && conversations.length && args.expected_identity && !visibleMatch))
    throw new Error('outreach_target_changed');
  if (args.op === 'contact') {
    if (!detailJob || view.contact_job !== args.job_id || view.start_text !== '立即沟通') throw new Error('outreach_contact_button_changed');
    start.click(); return JSON.stringify({clicked:true});
  }
  if (!editor) throw new Error('outreach_composer_unavailable');
  if (args.op === 'fill') {
    if (view.editor_text && view.editor_text !== normalize(args.message)) throw new Error('outreach_existing_input_not_overwritten');
    editor.focus();
    if (editor.tagName === 'TEXTAREA') {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor,args.message);
      editor.dispatchEvent(new Event('input',{bubbles:true}));
    } else {
      const range=document.createRange();range.selectNodeContents(editor);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      document.execCommand('insertText',false,args.message);
    }
    return JSON.stringify({filled:normalize(editor.value ?? editor.innerText) === normalize(args.message)});
  }
  if (args.op === 'send') {
    if (view.editor_text !== normalize(args.message)) throw new Error('outreach_message_changed');
    const buttons=all('button.btn-send,div.send-message').filter(e=>!e.disabled && e.getAttribute('aria-disabled')!=='true' && !e.classList.contains('disabled'));
    if (buttons.length!==1) throw new Error('outreach_send_button_unavailable');
    buttons[0].click();return JSON.stringify({clicked:true});
  }
  throw new Error('unsupported_outreach_action');
})"""


async def dom(session, op="read", *, tab=None, **data):
    if tab is not None and op != "read":
        raise Stopped("outreach_write_requires_current_tab")
    if op != "read":
        await session.pace.before()
        session.actions += 1
        ledger = Outreach(session.ws, session.store)
        ledger.require_authorization(data["job_id"], data["review_id"], data["authorization_id"])
        review = ledger.ready(data["job_id"], data["review_id"])
        if review["payload"]["greeting_draft"] != data["message"]:
            raise Stopped("outreach_message_changed")
    try:
        raw = await asyncio.wait_for((tab or session.tab).evaluate(DOM + "(" + json.dumps({"op": op, **data}, ensure_ascii=False) + ")",
                                                         return_by_value=True), 20)
    except asyncio.TimeoutError as exc:
        raise Stopped("browser_read_timeout") from exc
    except Exception as exc:
        from .runtime import connection_error
        if connection_error(exc):
            raise Stopped("browser_connection_lost:" + type(exc).__name__) from exc
        if op == "contact" and any(s in str(exc).lower() for s in (
                "execution context was destroyed", "cannot find context with specified id")):
            # A real navigation can destroy the click's context. Observe the destination; never click again.
            return {"clicked": False, "navigation_started": True}
        for reason in ("outreach_target_changed", "outreach_contact_button_changed", "outreach_composer_unavailable",
                       "outreach_existing_input_not_overwritten", "outreach_message_changed", "outreach_send_button_unavailable"):
            if reason in str(exc):
                raise Stopped(reason) from exc
        raise
    result = json.loads(raw)
    if result.get("limited"):
        raise Stopped("platform_contact_limit")
    return result


def correct(view, job_id, recipient):
    return view.get("job_id") == job_id and view.get("recipient") == recipient


def chat_matches(view, job_id, recipient, expected_identity=None):
    ids_match = correct(view, job_id, recipient)
    visible_match = visible_identity_matches(view, {"facts": expected_identity or {}})
    if view.get("conversation_count") and expected_identity and not visible_match:
        return False
    return ids_match or (not view.get("job_id") and not view.get("recipient") and visible_match)


def delivered(view, message):
    return [m for m in view.get("messages", []) if normalize_message(m.get("text", "")) == normalize_message(message)
            and not m.get("failed") and not m.get("pending")]


def normalize_message(message):
    return message.replace("\r\n", "\n").replace("\u00a0", " ").strip()


def observation(view, message):
    """Bounded diagnostic facts; no redirect tokens or unrelated conversation text."""
    view = view or {}
    return {**{k: view.get(k) for k in ("url", "job_id", "recipient", "surface", "modal_rows", "editor")},
            "identity": {k: str(v)[:200] for k, v in view.get("identity", {}).items()},
            "editor_empty": not view.get("editor_text"), "message_count": len(view.get("messages", [])),
            "matching_messages": [{k: m.get(k) for k in ("id", "source", "status", "failed", "pending")}
                                  for m in view.get("messages", [])
                                  if normalize_message(m.get("text", "")) == normalize_message(message)]}


async def wait_chat(session, job_id, recipient, timeout=15, message=None, old_tabs=None, expected_identity=None):
    last_view = None

    async def wait():
        nonlocal last_view
        while True:
            page = await session.read()
            problem = page_problem(page)
            if problem and problem not in ('blank_or_redirected', 'login_unconfirmed'):
                raise Stopped(problem)
            candidates = [session.tab]
            if old_tabs is not None:
                from urllib.parse import urlsplit
                await asyncio.wait_for(session.browser.update_targets(), 5)
                # A new parameterless chat tab is allowed only when its visible identity matches.
                # Never select an unrelated, pre-existing conversation.
                for tab in session.browser.tabs:
                    parts = urlsplit(tab.target.url)
                    if (tab.target.target_id not in old_tabs and tab is not session.tab
                            and parts.hostname == 'www.zhipin.com' and parts.path == '/web/geek/chat'):
                        candidates.append(tab)
            matches = []
            for tab in candidates:
                view = await dom(session, tab=tab)
                last_view = view
                if (chat_matches(view, job_id, recipient, expected_identity)
                        and (delivered(view, message) if message is not None else view.get("editor"))):
                    matches.append((tab, view))
            if len(matches) > 1:
                raise Stopped("outreach_multiple_matching_chats")
            if matches:
                session.tab, view = matches[0]
                return view
            await asyncio.sleep(0.5)
    try:
        return await asyncio.wait_for(wait(), timeout)
    except asyncio.TimeoutError as exc:
        raise ChatTimeout(last_view) from exc


async def send(session, request):
    ledger = Outreach(session.ws, session.store)
    existing = ledger.record(request["job_id"])
    if existing and existing["status"] in TERMINAL:
        return {"job_id": request["job_id"], "status": existing["status"], "skipped": True}
    ledger.require_authorization(request["job_id"], request["review_id"], request["authorization_id"])
    try:
        return await _send(session, request)
    except (Stopped, asyncio.TimeoutError) as exc:
        from .runtime import session_blocking
        if isinstance(exc, Stopped) and session_blocking(str(exc)):
            raise  # Browser/session health is not a per-job read failure.
        record = ledger.record(request["job_id"])
        if not record or record["status"] == "previewed" and record["review_id"] == request["review_id"]:
            # Before any contact attempt, record this ordinary read failure and continue other jobs.
            review = next((r for r in session.store.reviews() if r["id"] == request["review_id"]), None)
            if review:
                ledger.save(request["job_id"], review, "read_failed", {
                    "reason": str(exc) if isinstance(exc, Stopped) else "browser_operation_timeout",
                    "external_actions": 0})
        raise


async def _send(session, request):
    ledger = Outreach(session.ws, session.store)
    job_id = request["job_id"]
    review = ledger.ready(job_id, request["review_id"])
    ledger.require_authorization(job_id, review["id"], request["authorization_id"])
    existing = ledger.record(job_id)
    if existing and existing["status"] in TERMINAL:
        return {"job_id": job_id, "status": existing["status"], "skipped": True}
    job = session.store.job(job_id)
    await session.healthy_page()
    await session.action(job["url"])
    page = await session.settle(detail_id=job_id)
    problem = page_problem(page)
    if problem:
        raise Stopped(problem)
    if detail_state(page, job_id) != "complete":
        raise Stopped("detail_incomplete")
    if (page.get("chat_button") or "").strip() == "继续沟通":
        record = session.store.observe_existing_contact(job_id)
        return {"job_id": job_id, "status": record["status"], "skipped": True,
                "evidence": "detail_continue_button", "external_actions": 0}
    # Recheck the actual JD immediately before starting a conversation.
    detail = {k: page.get(k) for k in ("url", "job_detail", "company_info", "work_address", "chat_button", "recruiter_active_label")}
    detail["problem"] = None
    session.store.save_detail(job_id, detail, "complete")
    if session.store.job(job_id)["facts_hash"] != review["facts_hash"]:
        raise Stopped("job_changed_before_contact:review_saved_history_not_modified")
    review = ledger.ready(job_id, request["review_id"])
    activity = ledger.activity(job_id)
    if not activity["eligible"]:
        if activity["state"] != "deferred":
            raise Stopped("recruiter_activity_" + activity["state"])
        hold_state = "matched_inactive"
        session.store.suspend_match(job_id, review["id"], hold_state)
        result = {"recruiter_activity": activity, "next_action": "continue_queue", "external_actions": 0}
        ledger.save(job_id, review, hold_state, result)
        return {"job_id": job_id, "status": hold_state, **result}
    view = await dom(session)
    recipient = view.get("recipient")
    if not recipient or not correct(view, job_id, recipient) or view.get("contact_job") != job_id:
        raise Stopped("outreach_target_unverified")
    try:
        ledger.reserve(job_id, review, recipient)
    except Stopped as exc:
        if str(exc) != "recipient_already_contacted":
            raise
        ledger.save(job_id, review, "already_contacted", {"evidence": "recipient_ledger", "recipient": recipient})
        return {"job_id": job_id, "status": "already_contacted"}
    payload = {"job_id": job_id, "recipient": recipient, "message": review["payload"]["greeting_draft"],
               "review_id": review["id"], "authorization_id": request["authorization_id"],
               "expected_identity": {k: job["facts"].get(k) for k in ("company", "recruiter", "title")}}
    progress = {"phase": "contact_attempted", "send_attempted": False, "send_click_acknowledged": False}
    chat = view

    def phase(name, **values):
        progress.update(phase=name, **values)
        ledger.finish(job_id, "sending", progress)

    try:
        old_tabs = {t.target.target_id for t in session.browser.tabs}
        phase("contact_attempted")
        await dom(session, "contact", **payload)
        phase("waiting_chat")
        chat = await wait_chat(session, job_id, recipient, old_tabs=old_tabs, expected_identity=payload["expected_identity"])
        if delivered(chat, payload["message"]):
            return ledger.finish(job_id, "sent", {"evidence": "message_after_contact",
                                                 "messages": delivered(chat, payload["message"])})
        phase("filling_input")
        filled = await dom(session, "fill", **payload)
        if not filled.get("filled"):
            raise Stopped("outreach_input_not_verified")
        phase("send_attempted", send_attempted=True)
        clicked = await dom(session, "send", **payload)
        phase("waiting_delivery", send_click_acknowledged=bool(clicked.get("clicked")))
        chat = await wait_chat(session, job_id, recipient, timeout=DELIVERY_TIMEOUT, message=payload["message"],
                               expected_identity=payload["expected_identity"])
        return ledger.finish(job_id, "sent", {"evidence": "outgoing_message_in_target_chat",
                                              **progress, "phase": "confirmed",
                                              "messages": delivered(chat, payload["message"])})
    except asyncio.CancelledError:
        # Pause/close changes no job state: retain the last real attempt for verify.
        raise
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Stopped) else type(exc).__name__
        from .runtime import browser_problem, session_blocking
        if browser_problem(reason):
            raise
        if isinstance(exc, ChatTimeout):
            chat = exc.view
            reason = ("outreach_delivery_confirmation_timeout" if progress["phase"] == "waiting_delivery"
                      else "outreach_chat_open_timeout")
        result = ledger.finish(job_id, "uncertain", {"reason": reason, **progress,
                               "observation": observation(chat, payload["message"]),
                               "next_action": "verify_current_chat_do_not_resend"})
        if session_blocking(reason):
            raise
        return result


def visible_identity_matches(view, job):
    """Match the unique visible conversation and selected contact to known job facts."""
    if view.get("url") != "https://www.zhipin.com/web/geek/chat":
        return False
    identity, facts = view.get("identity", {}), job["facts"]
    return (all(facts.get(k) and identity.get(k) == normalize_message(facts[k]) for k in ("company", "recruiter", "title"))
            and identity.get("selected_recruiter") == normalize_message(facts["recruiter"])
            and identity.get("selected_company") == normalize_message(facts["company"]))


async def verify(session, request):
    ledger = Outreach(session.ws, session.store)
    job_id = request["job_id"]
    record = ledger.record(job_id)
    if not record or record["status"] not in ("sending", "uncertain"):
        raise Stopped("outreach_no_uncertain_attempt")
    await session.healthy_page()
    view = await dom(session)
    job = session.store.job(job_id)
    id_verified = (correct(view, job_id, record["recipient"])
                   and chat_matches(view, job_id, record["recipient"], job["facts"]))
    visible_verified = (not view.get("job_id") and not view.get("recipient")
                        and visible_identity_matches(view, job))
    if not (id_verified or visible_verified):
        raise Stopped("open_the_recorded_chat_before_read_only_verification")
    if delivered(view, record["message"]):
        return ledger.finish(job_id, "sent", {"evidence": "read_only_verification",
                                              "identity_evidence": "job_and_recipient_ids" if id_verified else "visible_company_recruiter_and_job",
                                              "previous_attempt": json.loads(record["result"]),
                                              "messages": delivered(view, record["message"])})
    return {"job_id": job_id, "status": "uncertain", "observation": observation(view, record["message"]),
            "next_action": "inspect_conversation_no_automatic_retry"}
