"""On-demand database reports; no persisted workflow or calibration cursor."""
from __future__ import annotations

import html
import math
import uuid
from pathlib import Path

from .local import ACTIVITY_WAITING, OUTREACH_ATTEMPTED, Stopped, digest, now, read_json, write_json


def review_view(job):
    """Lossless job facts for Agent review; no ranking or semantic filtering."""
    result = {key: job[key] for key in (
        "id", "url", "facts", "detail_state", "detail_seen", "detail_stale", "last_seen",
        "recruiter_activity", "activity_hold", "outreach_status")}
    detail = job["detail"]
    result["detail"] = {key: detail.get(key) for key in (
        "job_detail", "work_address", "company_info", "chat_button"
    )} if detail is not None else None
    return result


def snapshot(ledger, ids, partial_reason=None):
    reviews = {r["job_id"]: r for r in ledger.store.reviews()}
    items = []
    for job_id in dict.fromkeys(ids):
        job, review, record = ledger.store.job(job_id), reviews.get(job_id), ledger.record(job_id)
        data = review["payload"] if review else {}
        hold, activity = job["activity_hold"], ledger.activity(job_id)
        reviewed = bool(review and ledger.store.review_complete(review) and data.get("stage") == "detail")
        if record and record["status"] in OUTREACH_ATTEMPTED:
            result = record["status"]
        elif hold and hold["state"] in ACTIVITY_WAITING:
            result = hold["state"]
        elif record and record["status"] == "read_failed":
            result = "read_failed"
        elif data.get("decision") == "reject":
            result = "reject"
        elif not reviewed:
            result = "read_failed" if job["detail_state"] in ("failed", "partial", "unavailable") else "unreviewed"
        elif data.get("decision") == "hold":
            result = "reserve"
        elif not data.get("greeting_draft"):
            result = "prepare_greeting"
        elif job["facts_hash"] != review["facts_hash"]:
            result = "check_detail"
        elif activity["eligible"]:
            result = "send"
        else:
            result = "matched_inactive" if activity["state"] == "deferred" else "check_activity"
        items.append({"job_id": job_id, "facts": job["facts"], "url": job["url"],
                      "work_address": (job["detail"] or {}).get("work_address"),
                      "facts_hash": job["facts_hash"], "review_id": review["id"] if review else None,
                      "reviewed_at": review["created_at"] if review else None,
                      "detail_read": job["detail_state"] == "complete", "detail_reviewed": reviewed,
                      "decision": data.get("decision"), "result": result,
                      "reasons": data.get("reasons", []), "evidence": data.get("evidence", []),
                      "unknowns": data.get("unknowns", []), "recruiter_activity": activity,
                      "greeting_draft": data.get("greeting_draft") if result == "send" else None})
    if not items:
        raise Stopped("select_report_job_ids")
    counts = {"selected": len(items), "detail_read": sum(i["detail_read"] for i in items),
              "detail_reviewed": sum(i["detail_reviewed"] for i in items),
              "list_rejected": sum(i["result"] == "reject" and not i["detail_reviewed"] for i in items),
              "held": sum(i["result"] == "reserve" for i in items),
              "send_candidates": sum(i["result"] == "send" for i in items),
              "read_failed": sum(i["result"] == "read_failed" for i in items),
              "skill_sent": sum(i["result"] == "sent" for i in items),
              "contacted_external": sum(i["result"] == "contacted_external" for i in items),
              "contacted_dedup": sum(i["result"] == "already_contacted" for i in items),
              "uncertain": sum(i["result"] in ("sending", "uncertain") for i in items)}
    first = ledger.policy()["authorization"] is None
    step = ledger.ws.initial_review_count()
    target = max(step, math.ceil(counts["detail_reviewed"] / step) * step) if first else 0
    if first and counts["detail_reviewed"] >= target and not counts["send_candidates"]:
        target += step
    blockers = [i["job_id"] for i in items if i["result"] in ("unreviewed", "prepare_greeting", "check_detail", "check_activity")]
    ready = bool(counts["send_candidates"] and not blockers and
                 (counts["detail_reviewed"] >= target or partial_reason))
    counts["additional_reviews"] = max(0, target - counts["detail_reviewed"])
    signature = digest([{k: i[k] for k in ("job_id", "review_id", "facts_hash", "result", "greeting_draft")} for i in items])
    return {"items": items, "counts": counts, "target": target, "first_calibration": first,
            "blocking_ids": blockers, "ready_for_confirmation": ready, "signature": signature,
            "partial_reason": partial_reason, "external_actions": 0}


def create_preview(ledger, ids, partial_reason=None, out=None):
    ledger.ws.require_search_plan()
    result = snapshot(ledger, ids, partial_reason.strip() if partial_reason else None)
    identifier = uuid.uuid4().hex
    result.update(id=identifier, schema=3, workspace=str(ledger.ws.root), created_at=now())
    if out:
        report = Path(out).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_preview(result), encoding="utf-8")
        result["report_file"] = str(report)
    # Preserve the approval snapshot, independently of any optional Markdown export.
    write_json(ledger.root / "previews" / (identifier + ".json"), result)
    return result


LABELS = {"send": "合适，拟发送", "reject": "不合适", "reserve": "待定/备选",
          "matched_inactive": "合适不活跃", "matched_activity_unknown": "历史暂缓：活跃未知",
          "unreviewed": "尚未审完", "read_failed": "读取失败", "prepare_greeting": "尚未写招呼",
          "check_detail": "当前岗位已变化，历史判断保留", "check_activity": "联系前需更新活跃观察",
          "sent": "Skill 已发送", "contacted_external": "历史/其他方式已沟通", "already_contacted": "已沟通去重",
          "sending": "发送待核实", "uncertain": "发送待核实"}


def render_preview(preview):
    def safe(value):
        text = html.escape(str(value or "未提供")).replace("\\", "\\\\")
        for char in ("[", "]", "*", "_", "`", "#"):
            text = text.replace(char, "\\" + char)
        return text.replace("|", "&#124;").replace("\n", "<br>").replace("\r", "")

    counts = preview["counts"]
    lines = ["# 岗位审查与招呼预览", "",
             f"选入 {counts['selected']} 个；完整详情已审 {counts['detail_reviewed']} 个；"
             f"列表已排除 {counts.get('list_rejected', 0)} 个；待定 {counts.get('held', 0)} 个；"
             f"拟发送 {counts['send_candidates']} 个；读取失败 {counts['read_failed']} 个。", "",
             "| 序号 | 公司 / 岗位 | 工作地点 | 薪资 | 公司规模 | 活跃 | 结果 | 审核原因 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for index, item in enumerate(preview["items"], 1):
        facts = item["facts"]
        location = " / ".join(str(facts[k]) for k in ("city", "district") if facts.get(k))
        lines.append(f"| {index} | {safe(facts.get('company'))} / [{safe(facts.get('title'))}]({item['url']}) | "
                     f"{safe(location)} | {safe(facts.get('salary'))} | {safe(facts.get('company_size'))} | "
                     f"{safe(item['recruiter_activity'].get('label') or '未知')} | "
                     f"{LABELS[item['result']]} | {safe('；'.join(item['reasons']))} |")
    for index, item in enumerate(preview["items"], 1):
        lines += ["", f"## {index}. {safe(item['facts'].get('company'))} / {safe(item['facts'].get('title'))}", "",
                  "工作地址（详情）：" + safe(item.get("work_address")), "",
                  "结论：" + LABELS[item["result"]], "", "理由：" + safe("；".join(item["reasons"])), "",
                  "证据：" + safe("；".join(str(e.get("claim", "")) for e in item["evidence"])), "",
                  "待核实：" + safe("；".join(item["unknowns"]) or "无"), "", "招呼全文：", "",
                  "> " + safe(item["greeting_draft"]) if item["greeting_draft"] else "本岗位无拟发送招呼。"]
    if preview["partial_reason"]:
        lines += ["", "未能补齐的原因：" + safe(preview["partial_reason"])]
    lines += ["", "请先讨论尚未对齐的筛选口径，再校准结果与招呼，明确发送本批或继续本轮的授权范围。"
              if preview["ready_for_confirmation"] else
              f"以上结果可先用于讨论和校准，尚不具备发送确认条件。当前还差 {counts['additional_reviews']} 个完整审查，"
              "且需至少一个可联系岗位；有未对齐口径先澄清，无此问题再取新候选补审，勿重读列表已排除者凑数。", ""]
    return "\n".join(lines)


def progress(ledger):
    reviews = ledger.store.reviews()
    files = list((ledger.ws.runtime / "collections").glob("*.json"))
    latest = read_json(max(files, key=lambda p: p.stat().st_mtime)) if files else {}
    collection = {"run_id": latest.get("run_id"), "state": latest.get("state"), "reason": latest.get("reason"),
                  "searches": [{"key": q.get("key"), "state": q.get("state"), "reason": q.get("reason"),
                                "pages_collected": len(q.get("pages", [])), "skipped_rows": q.get("skipped_rows", 0)}
                               for q in latest.get("queries", [])]}
    return {"workspace": str(ledger.ws.root), **ledger.store.stats(),
            "reviewed": sum(ledger.store.review_complete(r) for r in reviews),
            "detail_reviewed": sum(ledger.store.review_complete(r) and r["payload"].get("stage") == "detail" for r in reviews),
            "queue_counts": ledger.queue()["counts"], "mode": ledger.policy()["mode"],
            "latest_collection": collection, "external_actions": 0}


def render_progress(result):
    labels = {"unreviewed": "待审", "draft_ready": "合适且有待发招呼", "reviewed": "已审，当前无待发招呼",
              "read_failed": "读取失败", "contacted": "已沟通", "uncertain": "发送待核实"}
    lines = ["# 求职进度", "", f"岗位总数：{result['jobs']}；已有审查结论：{result['reviewed']}。", "",
             "| 项目 | 数量 |", "| --- | --- |"]
    lines += [f"| {labels[k]} | {v} |" for k, v in result["queue_counts"].items()]
    lines += ["", "沟通记录：" + str(result["outreach"]), ""]
    collection = result["latest_collection"]
    if collection["run_id"]:
        lines += ["最近采集：" + str(collection["state"]), ""]
        lines += [f"- 搜索 {index}：已读 {q['pages_collected']} 页；{q['state']}；"
                  f"跳过 {q['skipped_rows']} 条；{html.escape(str(q['reason'] or '无异常原因'))}"
                  for index, q in enumerate(collection["searches"], 1)]
    return "\n".join(lines)
