"""Text and JSON rendering of analysis reports."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .analyze import Finding, Report, Severity, fmt_age

_COLORS = {
    Severity.ERROR: "\033[31m",
    Severity.WARNING: "\033[33m",
    Severity.INFO: "\033[36m",
    Severity.OK: "\033[32m",
}
_RESET = "\033[0m"


def _limit(max_events: int) -> int | None:
    return max_events or None


def _color(sev: Severity, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{_COLORS[sev]}{text}{_RESET}"


def render_text(report: Report, max_events: int, quiet: bool) -> str:
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for w in report.warnings:
        lines.append(f"note: {w}")
    shown = [f for f in report.findings if not (quiet and f.severity <= Severity.INFO)]
    for f in shown:
        lines.append(_render_finding(f, max_events, now))
    problems = sum(1 for f in report.findings if f.severity > Severity.INFO)
    if report.ok:
        lines.append(_color(Severity.OK, f"OK - all volumes in namespace {report.namespace!r} are fine"))
    else:
        lines.append(
            _color(Severity.ERROR, f"FAIL - {problems} object(s) with volume problems in namespace {report.namespace!r}")
        )
    return "\n".join(lines)


def _render_finding(f: Finding, max_events: int, now: datetime) -> str:
    head = f"{_color(f.severity, f'[{f.severity.name}]')} {f.kind}/{f.name}"
    if f.status:
        head += f" ({f.status})"
    out = [head]
    for issue in f.issues:
        out.append(f"    - {issue}")
    shown = f.events[: _limit(max_events)]
    for ev in shown:
        age = fmt_age(now - ev.last_seen) + " ago" if ev.last_seen else "unknown time"
        count = f" x{ev.count}" if ev.count > 1 else ""
        out.append(f"    {ev.type} {ev.reason}{count}, {age}: {ev.message}")
    if len(f.events) > len(shown):
        out.append(f"    ... {len(f.events) - len(shown)} older event(s), show with -e 0")
    return "\n".join(out) + "\n"


def render_json(report: Report, max_events: int) -> str:
    return json.dumps(
        {
            "namespace": report.namespace,
            "ok": report.ok,
            "warnings": report.warnings,
            "maxEvents": max_events or None,
            "objects": [
                {
                    "kind": f.kind,
                    "name": f.name,
                    "severity": f.severity.name,
                    "status": f.status,
                    "issues": f.issues,
                    "events": [
                        {
                            "type": e.type,
                            "reason": e.reason,
                            "message": e.message,
                            "count": e.count,
                            "lastSeen": e.last_seen.isoformat() if e.last_seen else None,
                        }
                        for e in f.events[: _limit(max_events)]
                    ],
                    "eventsTotal": len(f.events),
                    "eventsOmitted": max(0, len(f.events) - max_events) if max_events else 0,
                }
                for f in report.findings
            ],
        },
        indent=2,
    )
