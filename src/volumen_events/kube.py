"""Thin wrapper around kubectl (or OpenShift's oc), returning parsed JSON."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

Obj = dict[str, Any]


class KubectlError(Exception):
    pass


CLIS = ("kubectl", "oc")


def detect_cli() -> str:
    """Prefer kubectl, fall back to oc if only that is installed."""
    for cli in CLIS:
        if shutil.which(cli):
            return cli
    raise KubectlError("neither kubectl nor oc found in PATH")


@dataclass(frozen=True)
class Kubectl:
    context: str | None = None
    kubeconfig: str | None = None
    cli: str = "kubectl"

    def _base(self) -> list[str]:
        cmd = [self.cli]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        return cmd

    def get(
        self,
        resources: str,
        namespace: str | None = None,
        field_selector: str | None = None,
    ) -> list[Obj]:
        cmd = self._base() + ["get", resources, "-o", "json"]
        if namespace:
            cmd += ["-n", namespace]
        if field_selector:
            cmd += ["--field-selector", field_selector]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as e:
            raise KubectlError(f"{self.cli} not found in PATH") from e
        if proc.returncode != 0:
            raise KubectlError(proc.stderr.strip() or f"{' '.join(cmd)} failed")
        data: Obj = json.loads(proc.stdout)
        # A single named object is returned directly, a list as {"items": [...]}.
        if "items" in data:
            return list(data["items"])
        return [data]


@dataclass
class Snapshot:
    """Everything needed to judge the volumes of one namespace."""

    namespace: str
    pvcs: list[Obj] = field(default_factory=list)
    pods: list[Obj] = field(default_factory=list)
    statefulsets: list[Obj] = field(default_factory=list)
    events: list[Obj] = field(default_factory=list)
    pvs: list[Obj] = field(default_factory=list)
    storageclasses: list[Obj] = field(default_factory=list)
    pvs_loaded: bool = False
    warnings: list[str] = field(default_factory=list)


def check_namespace(kubectl: Kubectl, namespace: str) -> None:
    """Fail fast if the namespace is missing.

    OpenShift project members may not read the namespace object itself,
    but always their project, so that is tried as a fallback.
    """
    try:
        kubectl.get(f"namespace/{namespace}")
    except KubectlError as e:
        if "forbidden" not in str(e).lower():
            raise
        try:
            kubectl.get(f"project/{namespace}")
        except KubectlError:
            raise e from None


def load_snapshot(kubectl: Kubectl, namespace: str) -> Snapshot:
    check_namespace(kubectl, namespace)

    snap = Snapshot(namespace=namespace)
    for item in kubectl.get("persistentvolumeclaims,pods,statefulsets,events", namespace):
        kind = item.get("kind")
        if kind == "PersistentVolumeClaim":
            snap.pvcs.append(item)
        elif kind == "Pod":
            snap.pods.append(item)
        elif kind == "StatefulSet":
            snap.statefulsets.append(item)
        elif kind == "Event":
            snap.events.append(item)

    # Cluster-scoped objects may be forbidden for namespace-scoped users.
    try:
        snap.storageclasses = kubectl.get("storageclasses")
    except KubectlError as e:
        snap.warnings.append(f"cannot list storageclasses: {e}")
    try:
        snap.pvs = [
            pv
            for pv in kubectl.get("persistentvolumes")
            if (pv.get("spec", {}).get("claimRef") or {}).get("namespace") == namespace
        ]
        snap.pvs_loaded = True
    except KubectlError as e:
        snap.warnings.append(f"cannot list persistentvolumes: {e}")
    # Events of cluster-scoped PVs are recorded in the "default" namespace.
    if snap.pvs and namespace != "default":
        try:
            names = {pv["metadata"]["name"] for pv in snap.pvs}
            snap.events += [
                ev
                for ev in kubectl.get(
                    "events", "default", "involvedObject.kind=PersistentVolume"
                )
                if ev.get("involvedObject", {}).get("name") in names
            ]
        except KubectlError as e:
            snap.warnings.append(f"cannot list persistentvolume events: {e}")
    return snap
