"""Turn a namespace snapshot into per-object findings."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any

from .kube import Obj, Snapshot


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARNING = 2
    ERROR = 3


# Warning events on non-volume objects that are about volumes regardless of the message.
VOLUME_REASONS = {
    "FailedMount",
    "FailedUnMount",
    "FailedAttachVolume",
    "FailedDetachVolume",
    "FailedMapVolume",
    "FailedUnmapDevice",
    "VolumeResizeFailed",
    "FileSystemResizeFailed",
    "ProvisioningFailed",
    "FailedBinding",
    "ClaimLost",
    "ClaimMisbound",
    "VolumeMismatch",
    "VolumeFailedRecycle",
    "VolumeFailedDelete",
}
# Other warnings (FailedScheduling, FailedCreate, ...) count only if the message mentions volumes.
VOLUME_MESSAGE = re.compile(
    r"volume|persistentvolumeclaim|\bclaim\b|\bpvc\b|mount|attach|storageclass",
    re.IGNORECASE,
)
# Normal events on PVCs/PVs that mean "stuck" rather than "fine".
SUSPICIOUS_NORMAL = {
    "FailedBinding",
    "ExternalProvisioning",
    "ExternalExpanding",
    "WaitForPodScheduled",
    "Resizing",
    "FileSystemResizeRequired",
}
# Events about getting a volume for a claim; history once the claim is bound.
PROVISIONING_REASONS = {
    "ExternalProvisioning",
    "ProvisioningFailed",
    "FailedBinding",
    "WaitForFirstConsumer",
    "WaitForPodScheduled",
}
RESIZE_ERROR_CONDITIONS = {"ControllerResizeError", "NodeResizeError"}

_QUANTITY_SUFFIX = {
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}  # fmt: skip


def parse_quantity(value: str | None) -> float | None:
    if not value:
        return None
    m = re.fullmatch(r"([0-9.]+(?:[eE][-+]?[0-9]+)?)([a-zA-Z]*)", value.strip())
    if not m or (m[2] and m[2] not in _QUANTITY_SUFFIX):
        return None
    return float(m[1]) * _QUANTITY_SUFFIX.get(m[2], 1)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class Event:
    type: str
    reason: str
    message: str
    count: int
    last_seen: datetime | None

    @classmethod
    def from_obj(cls, ev: Obj) -> "Event":
        series: dict[str, Any] = ev.get("series") or {}
        meta = ev.get("metadata", {})
        last = (
            parse_time(series.get("lastObservedTime"))
            or parse_time(ev.get("lastTimestamp"))
            or parse_time(ev.get("eventTime"))
            or parse_time(ev.get("firstTimestamp"))
            or parse_time(meta.get("creationTimestamp"))
        )
        return cls(
            type=ev.get("type") or "Normal",
            reason=ev.get("reason") or "",
            message=(ev.get("message") or ev.get("note") or "").strip(),
            count=int(series.get("count") or ev.get("count") or 1),
            last_seen=last,
        )


@dataclass
class Finding:
    kind: str
    name: str
    severity: Severity = Severity.OK
    status: str = ""
    issues: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def add(self, severity: Severity, issue: str) -> None:
        self.severity = max(self.severity, severity)
        self.issues.append(issue)


@dataclass
class Report:
    namespace: str
    findings: list[Finding]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return all(f.severity <= Severity.INFO for f in self.findings)


def _claims_of_pod(pod: Obj) -> list[str]:
    claims: list[str] = []
    for vol in pod.get("spec", {}).get("volumes") or []:
        if pvc := vol.get("persistentVolumeClaim"):
            claims.append(pvc["claimName"])
        elif "ephemeral" in vol:
            claims.append(f"{pod['metadata']['name']}-{vol['name']}")
    return claims


def _pod_status(pod: Obj) -> str:
    phase = pod.get("status", {}).get("phase", "")
    claims = _claims_of_pod(pod)
    return f"{phase}, pvc: {', '.join(claims)}" if claims else phase


def _pod_ready_since(pod: Obj) -> datetime | None:
    """Start time of a pod whose containers all run, or None if it is not healthy.

    Returns datetime.min for healthy pods without usable timestamps.
    """
    status = pod.get("status", {})
    phase = status.get("phase")
    if phase == "Succeeded":
        return datetime.min.replace(tzinfo=UTC)
    if phase != "Running":
        return None
    started: list[datetime] = []
    for cs in status.get("containerStatuses") or []:
        state = cs.get("state", {})
        if "running" in state:
            if t := parse_time(state["running"].get("startedAt")):
                started.append(t)
        elif "terminated" not in state:
            return None
    return max(started, default=datetime.min.replace(tzinfo=UTC))


def analyze(
    snap: Snapshot,
    grace: timedelta = timedelta(seconds=30),
    since: timedelta | None = None,
    now: datetime | None = None,
) -> Report:
    now = now or datetime.now(UTC)
    findings: dict[tuple[str, str], Finding] = {}

    def finding(kind: str, name: str) -> Finding:
        return findings.setdefault((kind, name), Finding(kind, name))

    storageclasses = {sc["metadata"]["name"]: sc for sc in snap.storageclasses}
    default_sc = next(
        (
            name
            for name, sc in storageclasses.items()
            if sc["metadata"].get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true"
        ),
        None,
    )
    pvs = {pv["metadata"]["name"]: pv for pv in snap.pvs}
    pvc_names = {pvc["metadata"]["name"] for pvc in snap.pvcs}

    consumers: dict[str, list[str]] = {}
    for pod in snap.pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for claim in _claims_of_pod(pod):
            consumers.setdefault(claim, []).append(pod["metadata"]["name"])

    # --- PVC status ---
    for pvc in snap.pvcs:
        meta, spec, status = pvc["metadata"], pvc.get("spec", {}), pvc.get("status", {})
        name = meta["name"]
        f = finding("PersistentVolumeClaim", name)
        phase = status.get("phase", "Unknown")
        f.status = phase
        users = consumers.get(name, [])

        if meta.get("deletionTimestamp"):
            f.status = "Terminating"
            if users:
                f.add(Severity.ERROR, f"deletion blocked, still used by pod(s): {', '.join(users)}")
            else:
                f.add(Severity.ERROR, f"deletion pending, finalizers: {', '.join(meta.get('finalizers') or ['-'])}")

        if phase == "Lost":
            f.add(Severity.ERROR, f"bound PersistentVolume {spec.get('volumeName')!r} no longer exists")
        elif phase == "Pending":
            sc_name = spec.get("storageClassName")
            if sc_name is None:
                sc_name = default_sc
            age = now - (parse_time(meta.get("creationTimestamp")) or now)
            sc = storageclasses.get(sc_name or "")
            if sc_name and snap.storageclasses and sc is None:
                f.add(Severity.ERROR, f"StorageClass {sc_name!r} does not exist")
            elif not sc_name:
                sev = Severity.ERROR if age > grace else Severity.INFO
                f.add(sev, "no StorageClass, waiting for a matching pre-provisioned PersistentVolume")
            elif sc and sc.get("volumeBindingMode") == "WaitForFirstConsumer" and not users:
                f.add(Severity.INFO, "waiting for first consumer (no pod uses this claim yet)")
            elif age <= grace:
                f.add(Severity.INFO, f"provisioning (pending for {fmt_age(age)})")
            elif users:
                f.add(Severity.ERROR, f"still pending although used by pod(s): {', '.join(users)}")
            else:
                f.add(Severity.ERROR, f"still pending after {fmt_age(age)} (StorageClass {sc_name!r})")
        elif phase == "Bound":
            pv_name = spec.get("volumeName")
            pv = pvs.get(pv_name or "")
            if snap.pvs_loaded and pv_name and pv is None:
                f.add(Severity.ERROR, f"bound PersistentVolume {pv_name!r} not found")
            elif pv and pv.get("status", {}).get("phase") == "Failed":
                f.add(Severity.ERROR, f"PersistentVolume {pv_name!r} failed: {pv['status'].get('message', '')}")

            for cond in status.get("conditions") or []:
                if cond.get("status") != "True":
                    continue
                ctype = cond.get("type", "")
                msg = cond.get("message") or ""
                if ctype in RESIZE_ERROR_CONDITIONS:
                    f.add(Severity.ERROR, f"{ctype}: {msg}".rstrip(": "))
                elif ctype == "FileSystemResizePending":
                    f.add(Severity.WARNING, "filesystem resize pending (pod restart needed)")
                elif ctype in ("Resizing", "ModifyingVolume"):
                    f.add(Severity.WARNING, f"{ctype} in progress {msg}".strip())
                elif ctype == "ModifyVolumeError":
                    f.add(Severity.ERROR, f"{ctype}: {msg}".rstrip(": "))
            for res, state in (status.get("allocatedResourceStatuses") or {}).items():
                if state.endswith("Failed"):
                    f.add(Severity.ERROR, f"resize of {res}: {state}")
            requested = parse_quantity(spec.get("resources", {}).get("requests", {}).get("storage"))
            capacity = parse_quantity(status.get("capacity", {}).get("storage"))
            if requested and capacity and requested > capacity and not f.issues:
                f.add(
                    Severity.WARNING,
                    f"requested {spec['resources']['requests']['storage']} but capacity is "
                    f"{status['capacity']['storage']} (resize not completed)",
                )

    # --- PVs of this namespace ---
    for pv in snap.pvs:
        name = pv["metadata"]["name"]
        phase = pv.get("status", {}).get("phase", "Unknown")
        claim = (pv.get("spec", {}).get("claimRef") or {}).get("name")
        if phase == "Failed":
            f = finding("PersistentVolume", name)
            f.status = phase
            f.add(Severity.ERROR, pv["status"].get("message") or "volume failed")
        elif phase == "Released":
            f = finding("PersistentVolume", name)
            f.status = phase
            policy = pv.get("spec", {}).get("persistentVolumeReclaimPolicy")
            f.add(Severity.WARNING, f"claim {claim!r} was deleted, volume not reclaimed (policy {policy})")
        elif pv["metadata"].get("deletionTimestamp"):
            f = finding("PersistentVolume", name)
            f.status = "Terminating"
            f.add(Severity.WARNING, f"deletion pending, still bound to claim {claim!r}")

    # --- Pods referencing missing claims ---
    for pod in snap.pods:
        missing = [c for c in _claims_of_pod(pod) if c not in pvc_names]
        if missing and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed"):
            f = finding("Pod", pod["metadata"]["name"])
            f.status = _pod_status(pod)
            f.add(Severity.ERROR, f"references missing PersistentVolumeClaim(s): {', '.join(missing)}")

    # --- Events ---
    objects: dict[str, dict[str, Obj]] = {
        "PersistentVolumeClaim": {o["metadata"]["name"]: o for o in snap.pvcs},
        "Pod": {o["metadata"]["name"]: o for o in snap.pods},
        "StatefulSet": {o["metadata"]["name"]: o for o in snap.statefulsets},
        "PersistentVolume": pvs,
    }
    for raw in snap.events:
        involved = raw.get("involvedObject") or raw.get("regarding") or {}
        kind, name = involved.get("kind", ""), involved.get("name", "")
        ev = Event.from_obj(raw)
        if since and ev.last_seen and now - ev.last_seen > since:
            continue

        if kind in ("PersistentVolumeClaim", "PersistentVolume"):
            relevant = ev.type != "Normal" or ev.reason in SUSPICIOUS_NORMAL
        else:
            relevant = ev.type != "Normal" and (
                ev.reason in VOLUME_REASONS or bool(VOLUME_MESSAGE.search(ev.message))
            )
        if not relevant:
            continue

        if kind in objects:
            obj = objects[kind].get(name)
            # Event of an object that was deleted (or recreated under the same name).
            if obj is None or (involved.get("uid") and involved["uid"] != obj["metadata"].get("uid")):
                continue
            if _resolved(kind, obj, ev, findings.get((kind, name))):
                continue

        f = finding(kind, name)
        f.events.append(ev)
        if f.severity < Severity.WARNING:
            f.severity = Severity.WARNING
        if not f.status and kind == "Pod":
            f.status = _pod_status(objects["Pod"][name])

    oldest = datetime.min.replace(tzinfo=UTC)
    for f in findings.values():
        f.events.sort(key=lambda e: e.last_seen or oldest, reverse=True)

    ordered = sorted((f for f in findings.values() if f.severity > Severity.OK), key=lambda f: (-f.severity, f.kind, f.name))
    return Report(snap.namespace, ordered, list(snap.warnings))


def _resolved(kind: str, obj: Obj, ev: Event, current: Finding | None) -> bool:
    """True if the object is healthy now, so the event is history."""
    match kind:
        case "PersistentVolumeClaim":
            if obj.get("status", {}).get("phase") != "Bound":
                return False
            return ev.reason in PROVISIONING_REASONS or current is None or not current.issues
        case "PersistentVolume":
            return obj.get("status", {}).get("phase") in ("Bound", "Available") and current is None
        case "Pod":
            ready = _pod_ready_since(obj)
            return ready is not None and (ev.last_seen is None or ev.last_seen <= ready)
        case "StatefulSet":
            status = obj.get("status", {})
            return status.get("readyReplicas", 0) >= obj.get("spec", {}).get("replicas", 1)
        case _:
            return False


def fmt_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"
