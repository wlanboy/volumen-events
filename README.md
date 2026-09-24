# volumen-events

Zeigt für einen Namespace die letzten Events, die auf Fehler oder Warnungen bei Volumes hindeuten, und prüft zusätzlich den Status aller PVCs, der zugehörigen PVs und der Pods, die sie mounten.

```bash
uv run volumen-events -n <namespace>
```

Exit-Codes: `0` = OK (alle Volumes in Ordnung), `1` = Probleme gefunden, `2` = Fehler (z. B. Namespace existiert nicht, kubectl/oc schlägt fehl).

Das Tool ruft `kubectl` auf und nutzt dessen Kubeconfig/Context. Ist kein `kubectl` installiert, wird `oc` (OpenShift) verwendet; mit `--cli oc` lässt sich `oc` erzwingen. Darf ein Projektmitglied unter OpenShift das Namespace-Objekt nicht lesen, prüft das Tool stattdessen, ob das Projekt existiert.

| Option | Bedeutung |
|---|---|
| `-n, --namespace` | zu prüfender Namespace (Pflicht) |
| `--cli kubectl\|oc` | aufzurufender Client (Default: `kubectl`, sonst `oc`) |
| `--context`, `--kubeconfig` | werden an kubectl/oc durchgereicht |
| `-e, --events N` | Events pro Objekt, `0` = alle (Default 3) |
| `--since 30m` | ältere Events ignorieren |
| `--grace 30s` | so lange gelten Pending-PVCs noch als „provisioning“ |
| `-o json` | maschinenlesbare Ausgabe (pro Objekt mit `eventsTotal` und `eventsOmitted`) |
| `-q` | INFO-Einträge ausblenden |

## Was geprüft wird

- **PVC-Status:** `Lost`, `Pending` (fehlende StorageClass, kein passendes PV, Provisioner reagiert nicht, Pending trotz Pod), `Terminating` (noch von Pods benutzt), Resize-Conditions und -Fehler, angeforderte Größe > Kapazität
- **PVs des Namespaces** (über `claimRef`): `Released`, `Failed`, hängendes Löschen
- **Pods**, die nicht existierende PVCs referenzieren
- **Events:** alle Warnings an PVCs/PVs, dazu Normal-Events, die auf Hängen hindeuten (`ExternalProvisioning`, `FailedBinding`, `ExternalExpanding`, …). Bei Pods, StatefulSets usw. Warnings mit volume-bezogenem Grund oder Text (`FailedMount`, `FailedAttachVolume`, `FailedScheduling … PersistentVolumeClaim`, `FailedCreate … exceeded quota`, …)

Events von Objekten, die nicht mehr existieren, werden ausgeblendet, ebenso Events, deren Ursache erledigt ist: Provisioning-Events eines gebundenen PVCs, Resize-Events eines PVCs ohne offenes Resize-Problem, Provisioning-/Reclaim-Events eines PVs, das wieder `Bound`/`Available` ist, Pod-Events von vor dem Start eines laufenden Pods und Events eines StatefulSets, das ready ist. Andere Warnings an gesunden PVCs/PVs (z. B. `VolumeConditionAbnormal`, `VolumeModifyFailed`) bleiben sichtbar. Ein ungenutztes PVC mit `WaitForFirstConsumer` erscheint nur als INFO und zählt als OK.

## Testumgebung

```bash
./test.sh volev-test        # alle Szenarien (gesund + kaputt)
./test.sh volev-ok ok       # nur gesunde Szenarien, Tool muss OK liefern
uv run volumen-events -n volev-test
./uninstall.sh volev-test
```

Die Skripte nehmen `kubectl`, ohne kubectl `oc`; erzwingen lässt sich das mit `KUBE_CLI=oc ./test.sh …`.

`test.sh` erzeugt 1Mi-PVCs und `pause`-Pods für diese Fälle: gesundes PVC, ungenutztes WFFC-PVC, nicht existierende StorageClass, no-provisioner ohne PVs, fehlender Provisioner (+ wartender Pod), statisches Binding ohne passendes PV, RWX bei local-path, RWOP-Konflikt mit zwei Pods, Pod mit fehlendem PVC, StatefulSet durch ResourceQuota blockiert, PV-NodeAffinity auf nicht existierenden Node, hostPath- und local-PV mit fehlendem Pfad, fehlende ConfigMap/Secret-Volumes, hängender Resize, `Lost`-Claim, `Released`-PV und ein PVC, das gelöscht wurde, während ein Pod es noch benutzt.

Cluster-weite Hilfsobjekte (StorageClasses, PVs) heißen `volev-<namespace>-*` und tragen das Label `volumen-events/test-namespace=<namespace>`. `uninstall.sh` löscht nur Namespaces mit diesem Label. Getestet auf kind mit `rancher.io/local-path`.
