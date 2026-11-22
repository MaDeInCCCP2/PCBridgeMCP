"""Select an available backend without dispatching input or relaxing constraints."""
from statistics import median


def plan(backends, target, intent, avoid_physical_input=True, avoid_focus=True):
    if target not in ("browser", "desktop", "win32_desktop", "native") or intent not in ("observe", "act"):
        raise ValueError("Choose target browser, desktop, win32_desktop or native, and intent observe or act.")
    action = intent == "act"
    candidates = []

    def add(backend, tool, *, physical=False, focus=False, **details):
        if (avoid_focus and focus) or (avoid_physical_input and physical):
            return
        candidates.append({"backend": backend, "tool": tool, "physical_input": physical,
                           "may_change_focus": focus, **details})

    if target == "browser" and backends["browser"]["available"]:
        if not action or backends["browser"]["input_enabled"]:
            add("browser", "browser_action" if action else "browser_snapshot",
                prerequisite={"tool": "browser_session_open", "headless": True})
    if target == "native" and not action and backends["native"]["tools"]:
        add("native", "list_windows")
    if target == "win32_desktop":
        desktop = backends.get("win32_desktop", {})
        if desktop.get("available") and (not action or desktop.get("input_enabled")):
            add("win32_desktop", "desktop_action" if action else "desktop_snapshot", experimental=True,
                prerequisite={"tool": "desktop_session_open", "requires_approved_app": True})
    if target == "desktop":
        if backends["uia"]["available"] and (not action or backends["uia"]["input_enabled"]):
            add("uia", "ui_action" if action else "ui_find_live", focus=action)
        if backends["visual"]["available"] and (not action or backends["visual"]["input_enabled"]):
            add("visual", "computer_run_steps" if action else "computer_observe",
                physical=action, focus=action)

    return {"candidates": candidates, "selected": candidates[0]["backend"] if candidates else None}


def rank_by_history(candidates, events, cutoff, minimum=5):
    """Estimate reliability of permitted candidates from the caller's observed tool outcomes."""
    evidence = []
    for candidate in candidates:
        recent = [event for event in events if event["ts"] >= cutoff and event["tool"] == candidate["tool"]]
        durations = [event["duration_ms"] for event in recent if event["outcome"] == "ok"]
        evidence.append({**candidate, "samples": len(recent), "successes": len(durations),
                         "median_success_ms": median(durations) if durations else None})
    ranked = len(evidence) > 1 and all(row["samples"] >= minimum for row in evidence)
    if ranked:
        evidence.sort(key=lambda row: (-(row["successes"] / row["samples"]),
                                       float("inf") if row["median_success_ms"] is None else row["median_success_ms"]))
    return {"candidates": evidence, "selected": evidence[0]["backend"] if evidence else None,
            "ranking": "history" if ranked else "deterministic"}
