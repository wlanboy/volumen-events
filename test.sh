#!/usr/bin/env bash
# Creates small PVCs/pods in a test namespace that cover as many volume failure modes as possible.
#
#   ./test.sh <namespace>        all scenarios (healthy + broken)
#   ./test.sh <namespace> ok     only healthy scenarios -> volumen-events must print OK
#
# Cluster-scoped helpers (StorageClasses, PVs) are named volev-<namespace>-* and labelled
# volumen-events/test-namespace=<namespace>. Remove everything with ./uninstall.sh <namespace>.
set -euo pipefail

NS="${1:?usage: $0 <namespace> [all|ok]}"
MODE="${2:-all}"
P="volev-${NS}"
LABEL="volumen-events/test-namespace: \"${NS}\""
IMAGE="registry.k8s.io/pause:3.10"

# Use oc if kubectl is not installed (override with KUBE_CLI=oc).
KUBE_CLI="${KUBE_CLI:-$(command -v kubectl >/dev/null && echo kubectl || echo oc)}"
kubectl() { command "${KUBE_CLI}" "$@"; }

step() { printf '\n==> %s\n' "$*"; }
apply() { kubectl apply -f - >/dev/null; }

# pod <name> <claim> [extra volumes yaml]: tiny pause pod mounting one PVC
pod() {
  apply <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: ${NS}
  labels: {${LABEL}}
spec:
  terminationGracePeriodSeconds: 0
  containers:
    - name: pause
      image: ${IMAGE}
      resources: {requests: {cpu: 1m, memory: 4Mi}}
      volumeMounts: [{name: data, mountPath: /data}]
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: $2}
EOF
}

# pvc <name> <storageClassName|- for default class> [accessMode] [extra spec yaml]
pvc() {
  local sc=""
  [[ "$2" != "-" ]] && sc="storageClassName: \"$2\""
  apply <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $1
  namespace: ${NS}
  labels: {${LABEL}}
spec:
  ${sc}
  accessModes: [${3:-ReadWriteOnce}]
  resources: {requests: {storage: 1Mi}}
  ${4:-}
EOF
}

# static_pv <name> <claim> <volume source yaml> [nodeAffinity yaml]
static_pv() {
  apply <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: $1
  labels: {${LABEL}}
spec:
  storageClassName: ""
  capacity: {storage: 1Mi}
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  claimRef: {namespace: ${NS}, name: $2}
  $3
  ${4:-}
EOF
}

wait_phase() { # wait_phase <kind/name> <phase>
  kubectl -n "${NS}" wait --for=jsonpath='{.status.phase}'="$2" "$1" --timeout=90s >/dev/null
}

step "namespace ${NS}"
kubectl create namespace "${NS}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace "${NS}" volumen-events/test-namespace="${NS}" --overwrite >/dev/null

# ---------------------------------------------------------------- healthy
step "healthy: bound PVC with running pod (healthy-data)"
pvc healthy-data -
pod healthy-pod healthy-data

step "healthy: unused PVC waiting for first consumer (unused-wffc, INFO only)"
pvc unused-wffc standard

if [[ "${MODE}" == "ok" ]]; then
  wait_phase pod/healthy-pod Running
  step "done (ok mode) - run: uv run volumen-events -n ${NS}"
  exit 0
fi

NODE="$(kubectl get nodes -l '!node-role.kubernetes.io/control-plane' -o jsonpath='{.items[0].metadata.name}')"
[[ -z "${NODE}" ]] && NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"

# ---------------------------------------------------------------- cluster-scoped helpers
step "storage classes"
apply <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: ${P}-no-provisioner, labels: {${LABEL}}}
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: Immediate
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: ${P}-missing-provisioner, labels: {${LABEL}}}
provisioner: example.com/does-not-exist
volumeBindingMode: Immediate
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: ${P}-expandable, labels: {${LABEL}}}
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: ${P}-quota, labels: {${LABEL}}}
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
EOF

# ---------------------------------------------------------------- provisioning / binding
step "StorageClass does not exist (sc-missing)"
pvc sc-missing "${P}-does-not-exist"

step "no-provisioner class without PVs, Immediate binding (no-pv-available)"
pvc no-pv-available "${P}-no-provisioner"

step "provisioner not running (provisioner-missing) + pod waiting for it (unbound-pvc-pod)"
pvc provisioner-missing "${P}-missing-provisioner"
pod unbound-pvc-pod provisioner-missing

step "static binding, selector matches no PV (static-no-match)"
pvc static-no-match "" ReadWriteOnce "selector: {matchLabels: {volev: no-such-pv}}"

step "access mode ReadWriteMany not supported by local-path (rwx-unsupported)"
pvc rwx-unsupported standard ReadWriteMany
pod rwx-pod rwx-unsupported

step "ReadWriteOncePod claim used by two pods (rwop-conflict)"
pvc rwop-conflict standard ReadWriteOncePod
pod rwop-pod-1 rwop-conflict

step "pod references a PVC that does not exist (missing-pvc-pod)"
pod missing-pvc-pod does-not-exist

step "StatefulSet blocked by ResourceQuota on its claims (quota-sts)"
apply <<EOF
apiVersion: v1
kind: ResourceQuota
metadata: {name: volev-no-claims, namespace: ${NS}, labels: {${LABEL}}}
spec:
  hard:
    ${P}-quota.storageclass.storage.k8s.io/persistentvolumeclaims: "0"
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: quota-sts, namespace: ${NS}, labels: {${LABEL}}}
spec:
  replicas: 1
  serviceName: quota-sts
  selector: {matchLabels: {app: quota-sts}}
  template:
    metadata: {labels: {app: quota-sts}}
    spec:
      terminationGracePeriodSeconds: 0
      containers:
        - name: pause
          image: ${IMAGE}
          resources: {requests: {cpu: 1m, memory: 4Mi}}
          volumeMounts: [{name: data, mountPath: /data}]
  volumeClaimTemplates:
    - metadata: {name: data}
      spec:
        storageClassName: ${P}-quota
        accessModes: [ReadWriteOnce]
        resources: {requests: {storage: 1Mi}}
EOF

# ---------------------------------------------------------------- scheduling / mounting
step "PV node affinity points to a non-existing node (affinity-conflict)"
static_pv "${P}-affinity" affinity-conflict \
  "local: {path: /tmp}" \
  "nodeAffinity: {required: {nodeSelectorTerms: [{matchExpressions: [{key: kubernetes.io/hostname, operator: In, values: [no-such-node]}]}]}}"
pvc affinity-conflict "" ReadWriteOnce "volumeName: ${P}-affinity"
pod affinity-pod affinity-conflict

step "hostPath PV with missing directory (hostpath-missing)"
static_pv "${P}-hostpath" hostpath-missing "hostPath: {path: /volev-does-not-exist, type: Directory}"
pvc hostpath-missing "" ReadWriteOnce "volumeName: ${P}-hostpath"
pod hostpath-pod hostpath-missing

step "local PV with missing path on ${NODE} (local-path-missing)"
static_pv "${P}-local" local-path-missing \
  "local: {path: /volev-does-not-exist}" \
  "nodeAffinity: {required: {nodeSelectorTerms: [{matchExpressions: [{key: kubernetes.io/hostname, operator: In, values: [${NODE}]}]}]}}"
pvc local-path-missing "" ReadWriteOnce "volumeName: ${P}-local"
pod local-pod local-path-missing

step "configMap and secret volumes that do not exist (configmap-pod, secret-pod)"
apply <<EOF
apiVersion: v1
kind: Pod
metadata: {name: configmap-pod, namespace: ${NS}, labels: {${LABEL}}}
spec:
  terminationGracePeriodSeconds: 0
  containers:
    - name: pause
      image: ${IMAGE}
      resources: {requests: {cpu: 1m, memory: 4Mi}}
      volumeMounts: [{name: cfg, mountPath: /cfg}]
  volumes:
    - name: cfg
      configMap: {name: volev-does-not-exist}
---
apiVersion: v1
kind: Pod
metadata: {name: secret-pod, namespace: ${NS}, labels: {${LABEL}}}
spec:
  terminationGracePeriodSeconds: 0
  containers:
    - name: pause
      image: ${IMAGE}
      resources: {requests: {cpu: 1m, memory: 4Mi}}
      volumeMounts: [{name: sec, mountPath: /sec}]
  volumes:
    - name: sec
      secret: {secretName: volev-does-not-exist}
EOF

# ---------------------------------------------------------------- lifecycle problems
step "resize requested but no resizer running (resize-pending)"
pvc resize-pending "${P}-expandable"
pod resize-pod resize-pending

step "claim whose PV gets deleted (claim-lost)"
static_pv "${P}-lost" claim-lost "hostPath: {path: /tmp/volev-lost, type: DirectoryOrCreate}"
pvc claim-lost "" ReadWriteOnce "volumeName: ${P}-lost"

step "Retain PV whose claim gets deleted (released-claim)"
static_pv "${P}-released" released-claim "hostPath: {path: /tmp/volev-released, type: DirectoryOrCreate}"
pvc released-claim "" ReadWriteOnce "volumeName: ${P}-released"

step "PVC deleted while a pod still uses it (in-use-deleted)"
pvc in-use-deleted standard
pod in-use-pod in-use-deleted

step "waiting for bindings"
wait_phase pod/healthy-pod Running
wait_phase pvc/rwop-conflict Bound
pod rwop-pod-2 rwop-conflict
wait_phase pvc/resize-pending Bound
wait_phase pvc/claim-lost Bound
wait_phase pvc/released-claim Bound
wait_phase pod/in-use-pod Running

step "triggering lifecycle problems"
kubectl -n "${NS}" patch pvc resize-pending -p '{"spec":{"resources":{"requests":{"storage":"2Mi"}}}}' >/dev/null
kubectl delete pv "${P}-lost" --wait=false >/dev/null
kubectl patch pv "${P}-lost" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null
kubectl -n "${NS}" delete pvc released-claim --wait=true >/dev/null
kubectl -n "${NS}" delete pvc in-use-deleted --wait=false >/dev/null

step "done - events need a few seconds to appear, then run: uv run volumen-events -n ${NS}"
