import json
import os
import threading
from datetime import datetime, timezone

from scan_target import scan_target


_lock = threading.Lock()


def _snapshot_path():
    return os.getenv(
        "PRAETOR_MONITOR_SNAPSHOT_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_snapshots.json"),
    )


def _load_snapshots():
    try:
        with open(_snapshot_path(), "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def _save_snapshots(snapshots):
    path = _snapshot_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(snapshots, file, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _diff(previous, current):
    if not previous:
        return {
            "summary": "first_scan",
            "score_change": None,
            "risk_level_changed": False,
            "new_findings": current.get("findings") or [],
            "resolved_findings": [],
        }

    previous_findings = set(previous.get("findings") or [])
    current_findings = set(current.get("findings") or [])
    score_change = (current.get("risk_score") or 0) - (previous.get("risk_score") or 0)
    new_findings = sorted(current_findings - previous_findings)
    resolved_findings = sorted(previous_findings - current_findings)

    changes = []
    if score_change:
        changes.append(f"score_change={score_change}")
    if previous.get("risk_level") != current.get("risk_level"):
        changes.append(f"risk_level={previous.get('risk_level')}->{current.get('risk_level')}")
    if new_findings:
        changes.append(f"new_findings={len(new_findings)}")
    if resolved_findings:
        changes.append(f"resolved_findings={len(resolved_findings)}")

    return {
        "summary": ", ".join(changes) if changes else "no_material_change",
        "score_change": score_change,
        "risk_level_changed": previous.get("risk_level") != current.get("risk_level"),
        "new_findings": new_findings,
        "resolved_findings": resolved_findings,
    }


def run_monitor(domain, depth="pro"):
    current = scan_target(domain, depth=depth)
    current["monitored_at"] = datetime.now(timezone.utc).isoformat()

    with _lock:
        snapshots = _load_snapshots()
        previous = snapshots.get(domain)
        snapshots[domain] = current
        _save_snapshots(snapshots)

    return {
        "domain": domain,
        "depth": depth,
        "is_first_scan": previous is None,
        "current": current,
        "previous": previous,
        "diff": _diff(previous, current),
    }
