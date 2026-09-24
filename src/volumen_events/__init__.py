"""Show recent warning/error events for all volumes of a Kubernetes namespace."""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta

from .analyze import Finding, Report, Severity, analyze, fmt_age
from .kube import Kubectl, KubectlError, load_snapshot

EXIT_OK, EXIT_PROBLEMS, EXIT_FAILURE = 0, 1, 2

_COLORS = {
    Severity.ERROR: "\033[31m",
    Severity.WARNING: "\033[33m",
    Severity.INFO: "\033[36m",
    Severity.OK: "\033[32m",
}
_RESET = "\033[0m"


def parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smhd]?)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {value!r} (e.g. 30s, 15m, 2h, 1d)")
    unit = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
    return timedelta(seconds=int(m[1]) * unit)


def non_negative_int(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"invalid count {value!r} (0 = all)")
    return int(value)


def _limit(max_events: int) -> int | None:
    return max_events or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="volumen-events",
        description="Show recent events pointing to errors or warnings for all volumes "
        "(PVCs, their PVs and the pods mounting them) in a namespace. "
        "Prints OK and exits 0 if all volumes are fine, exits 1 on problems, 2 on errors.",
    )
    p.add_argument("-n", "--namespace", required=True, help="namespace to check")
    p.add_argument("--context", help="kubeconfig context")
    p.add_argument("--kubeconfig", help="path to kubeconfig")
    p.add_argument(
        "-e",
        "--events",
        type=non_negative_int,
        default=3,
        metavar="N",
        help="events shown per object, 0 = all (default 3)",
    )
    p.add_argument("--since", type=parse_duration, metavar="DUR", help="ignore events older than DUR (e.g. 30m)")
    p.add_argument(
        "--grace",
        type=parse_duration,
        default=timedelta(seconds=30),
        metavar="DUR",
        help="pending PVCs younger than this count as provisioning (default 30s)",
    )
    p.add_argument("-o", "--output", choices=["text", "json"], default="text")
    p.add_argument("-q", "--quiet", action="store_true", help="hide INFO findings")
    return p


def _color(sev: Severity, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{_COLORS[sev]}{text}{_RESET}"


def render_text(report: Report, max_events: int, quiet: bool) -> str:
    now = datetime.now(UTC)
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


def main() -> None:
    args = build_parser().parse_args()
    kubectl = Kubectl(context=args.context, kubeconfig=args.kubeconfig)
    try:
        snap = load_snapshot(kubectl, args.namespace)
    except KubectlError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)

    report = analyze(snap, grace=args.grace, since=args.since)
    if args.output == "json":
        print(render_json(report, args.events))
    else:
        print(render_text(report, args.events, args.quiet))
    sys.exit(EXIT_OK if report.ok else EXIT_PROBLEMS)
