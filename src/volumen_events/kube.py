"""Thin wrapper around kubectl, returning parsed JSON."""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

type Obj = dict[str, Any]


class KubectlError(Exception):
    pass


@dataclass(frozen=True)
class Kubectl:
    context: str | None = None
    kubeconfig: str | None = None

    def _base(self) -> list[str]:
        cmd = ["kubectl"]
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
            raise KubectlError("kubectl not found in PATH") from e
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


def load_snapshot(kubectl: Kubectl, namespace: str) -> Snapshot:
    kubectl.get(f"namespace/{namespace}")  # fails fast if the namespace is missing

    snap = Snapshot(namespace=namespace)
    for item in kubectl.get("persistentvolumeclaims,pods,statefulsets,events", namespace):
        match item.get("kind"):
            case "PersistentVolumeClaim":
                snap.pvcs.append(item)
            case "Pod":
                snap.pods.append(item)
            case "StatefulSet":
                snap.statefulsets.append(item)
            case "Event":
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
