"""Save a small set of Agent-written decisions, without packets or task state."""
import json
from pathlib import Path

from .local import Stopped


def submit_reviews(ws, store, path):
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    items = payload.get("items", [payload]) if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise Stopped("review_items_required")
    results = []
    for item in items:
        job_id = item.get("job_id") if isinstance(item, dict) else None
        try:
            if not isinstance(job_id, str) or not job_id:
                raise Stopped("review_job_id_required")
            data = dict(item)
            if data.get("stage") == "list" and data.get("decision") == "shortlist":
                raise Stopped("read_detail_before_shortlisting")
            if data.get("stage") == "list" and data.get("decision") == "hold":
                data.setdefault("next_action", "fetch_detail")
            results.append(store.review(data, cache_hours=ws.settings["detail_cache_hours"], activity_policy=ws.outreach_activity()))
        except (Stopped, KeyError, ValueError, TypeError) as exc:
            failure = {"job_id": job_id, "saved": False, "error": str(exc)}
            if str(exc) in ("invalid_review_decision", "invalid_review_stage"):
                failure["hint"] = "stage must be list/detail; decision must be shortlist/hold/reject. Fix and resubmit this item only."
            results.append(failure)
    saved = sum(r.get("saved", False) for r in results)
    failed_ids = [r["job_id"] for r in results if "error" in r]
    return {"state": "saved" if not failed_ids else "partial" if saved else "failed",
            "saved": saved, "failed": len(failed_ids), "failed_ids": failed_ids,
            "items": results, "external_actions": 0}
