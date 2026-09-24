"""Command line interface of volumen-events."""

import argparse
import re
import sys
from datetime import timedelta

from .analyze import analyze
from .kube import CLIS, Kubectl, KubectlError, detect_cli, load_snapshot
from .render import render_json, render_text

EXIT_OK, EXIT_PROBLEMS, EXIT_FAILURE = 0, 1, 2


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="volumen-events",
        description="Show recent events pointing to errors or warnings for all volumes "
        "(PVCs, their PVs and the pods mounting them) in a namespace. "
        "Prints OK and exits 0 if all volumes are fine, exits 1 on problems, 2 on errors.",
    )
    p.add_argument("-n", "--namespace", required=True, help="namespace to check")
    p.add_argument("--cli", choices=CLIS, help="client binary to call (default: kubectl, else oc)")
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


def main() -> None:
    args = build_parser().parse_args()
    try:
        kubectl = Kubectl(context=args.context, kubeconfig=args.kubeconfig, cli=args.cli or detect_cli())
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
