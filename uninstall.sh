#!/usr/bin/env bash
# Removes everything test.sh created for a namespace: the namespace itself and the
# cluster-scoped StorageClasses/PVs labelled volumen-events/test-namespace=<namespace>.
set -euo pipefail

NS="${1:?usage: $0 <namespace>}"
SEL="volumen-events/test-namespace=${NS}"

if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  if [[ "$(kubectl get namespace "${NS}" -o jsonpath="{.metadata.labels['volumen-events/test-namespace']}")" != "${NS}" ]]; then
    echo "namespace ${NS} was not created by test.sh (label ${SEL} missing), refusing to delete it" >&2
    exit 1
  fi
  echo "==> deleting namespace ${NS}"
  kubectl delete namespace "${NS}" --wait=false >/dev/null
  # Pods first so pvc-protection lets the claims go.
  kubectl -n "${NS}" delete pods --all --grace-period=0 --force >/dev/null 2>&1 || true
  if ! kubectl wait --for=delete "namespace/${NS}" --timeout=120s >/dev/null 2>&1; then
    echo "==> namespace still terminating, removing PVC finalizers"
    for pvc in $(kubectl -n "${NS}" get pvc -o name 2>/dev/null); do
      kubectl -n "${NS}" patch "${pvc}" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null || true
    done
    kubectl wait --for=delete "namespace/${NS}" --timeout=60s >/dev/null
  fi
fi

echo "==> deleting PersistentVolumes (${SEL})"
for pv in $(kubectl get pv -l "${SEL}" -o name); do
  kubectl delete "${pv}" --wait=false >/dev/null
  kubectl patch "${pv}" --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 || true
done

echo "==> deleting StorageClasses (${SEL})"
kubectl delete storageclass -l "${SEL}" --ignore-not-found >/dev/null

echo "==> done"
