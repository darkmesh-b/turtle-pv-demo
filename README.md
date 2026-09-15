# Slow & steady

A browser demo of continuous writes to a Kubernetes persistent volume. A turtle
walks a maze and collects lettuces. **Every step is committed before the browser
can display it.** Close the browser and the turtle keeps going. Replace the pod
and it resumes its saved journey.

The dashboard shows the maze, recent trail, lettuce total, saved-step counter,
resume count, current pod, PVC, transaction time and recent database events.
The garden ID distinguishes the saved dataset from a freshly initialised volume.

## Deploy: no image build required

Requires a cluster with a default CSI StorageClass (or set `storageClassName`
under the PVC), permission to create a namespace and workloads, and access to
the `python:3.12-slim` image. Mirror that image into your registry if needed.
The Python and HTML sources are embedded in a ConfigMap. No pip downloads,
external fonts, JavaScript packages, database service or ingress controller.

For ordinary Kubernetes / VKS:

```bash
kubectl apply -f k8s/turtle-pv-demo.yaml
kubectl -n turtle-demo rollout status deployment/turtle
kubectl -n turtle-demo port-forward service/turtle 8080:80
```

Open <http://localhost:8080>.

For OpenShift, **use the OpenShift manifest instead**:

```bash
oc apply -f k8s/turtle-pv-demo-openshift.yaml
oc -n turtle-demo rollout status deployment/turtle
oc -n turtle-demo expose service turtle
oc -n turtle-demo get route turtle
```

Open the route's HTTP hostname. This manifest specifies neither `runAsUser` nor
`fsGroup`: the standard restricted SCC supplies the namespace-appropriate values.
No `anyuid` or privileged SCC is required by the app. The generic Kubernetes
manifest uses UID/GID/fsGroup 10001 for a writable volume; use only one variant.
Actual volume ownership handling depends on the CSI driver and cluster policy.

The demo has no authentication and provides only read endpoints. Keep the route
or LoadBalancer within your demo environment. Terminate TLS at your usual ingress
if required.

## A useful 60-second demonstration

1. Open the garden. Let the turtle collect some lettuces.
2. Note the garden ID, steps saved, lettuce count and current pod.
3. Replace the pod, leaving the PVC in place:

   ```bash
   kubectl -n turtle-demo rollout restart deployment/turtle
   kubectl -n turtle-demo rollout status deployment/turtle
   ```

4. With a Route, ingress or LoadBalancer, the browser reconnects automatically.
   If using `kubectl port-forward`, restart that command after replacement: the
   port-forward connection is tied to the old pod even when it targets a Service.
5. The pod name changes and `Times resumed` increases. The saved garden, position,
   trail, steps and lettuce count continue. Missed browser polls can make the
   turtle skip ahead to its latest committed position.

For a VKS lab with a load balancer, an optional way to keep the same browser URL:

```bash
kubectl -n turtle-demo patch service turtle -p '{"spec":{"type":"LoadBalancer"}}'
kubectl -n turtle-demo get service turtle
```

Browse the allocated address on port 80. Routes and LoadBalancers are optional;
neither is created by the base manifest.

## Demonstrating a PV migration

Scale the writer to zero and wait for its pod to terminate before your storage
migration procedure. Move or restore the volume, bind the destination PVC to the
migrated data, and start the same app pointed at that PVC. Ensure the destination
pod can write with its destination UID/fsGroup; this particularly matters when
moving between OpenShift namespaces with different assigned ranges.

```bash
kubectl -n turtle-demo scale deployment/turtle --replicas=0
kubectl -n turtle-demo wait --for=delete pod -l app=turtle --timeout=120s
# Perform your chosen PV migration / re-registration procedure here.
# Start the destination workload only after its PVC is correctly bound.
```

The garden ID and saved counters should match the source. This package does not
perform the migration itself. Ensure the destination uses the migrated PVC,
rather than letting it provision a new empty one. An empty directory deliberately
creates a new garden, while an unreadable or corrupt existing database fails
startup; errors never trigger an automatic reset.

## What is actually written

- The server moves one maze cell per iteration, with a default 1-second wait
  between completed iterations. Transaction time adds to that interval.
- A SQLite transaction updates the maze state, position, recent trail, lettuce
  locations, totals and saved timestamp, and inserts an audit event. Each lettuce
  collected is replaced with another on a reachable path, so the demo keeps going.
- The database is `/data/turtle.db`, where `/data` is the PVC mount.
- SQLite uses rollback journalling (`journal_mode=DELETE`) and
  `synchronous=EXTRA`: commit requests the relevant filesystem syncs, including
  the journal-directory sync. The UI reads only committed transactions.
- Storage must honour filesystem locking and sync semantics. This demonstrates
  application-level committed progress; it does not independently measure the
  storage hardware's durable writes or prove correctness of a live snapshot.
- The audit log keeps the latest 300 events; the trail keeps 100 positions.
  Lifetime step/lettuce totals remain saved. Database size stabilises, but writes
  continue. The step counter is a transaction count, **not block-level IOPS**.
- If writes fail, the server retries from committed state. The browser flags
  stale or unavailable state and does not invent forward progress.
- One writer only: `replicas: 1`, `Recreate`, plus an advisory file lock. Do not
  scale horizontally. This lock is not distributed fencing for a split-brain
  storage system. Prefer a normal CSI filesystem volume supporting POSIX locks.
- Runtime commit latency describes the most recent successful transaction in
  this process. `Times resumed` counts app starts after the first, not solely
  Kubernetes pod replacements.

Do not copy just a live `turtle.db` while it is being written. Quiesce first or
use a valid SQLite backup/snapshot procedure that preserves recovery data.

References: [SQLite sync modes](https://www.sqlite.org/pragma.html#pragma_synchronous),
[Kubernetes security contexts](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/),
[Deployment replacement strategy](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#recreate-deployment).

## Configure or edit

`STEP_SECONDS` controls the delay (minimum 0.1 seconds). `PORT` defaults to 8080.
`DATA_DIR`, `POD_NAME`, `NODE_NAME` and `PVC_NAME` provide storage and display
settings. The latter is a configured label, not a discovery call to Kubernetes.
No service-account token is mounted.

Edit `app.py` / `index.html`, then regenerate the embedded manifests:

```bash
python3 build_manifests.py
kubectl apply -f k8s/turtle-pv-demo.yaml
kubectl -n turtle-demo rollout restart deployment/turtle
```

Use the OpenShift variant when appropriate. A ConfigMap update alone does not
restart the Python process or reload the HTML it cached at startup.

An optional Dockerfile is included if you prefer a custom image:

```bash
docker build -t YOUR_REGISTRY/turtle-pv-demo:1.0 .
docker push YOUR_REGISTRY/turtle-pv-demo:1.0
```

To use that image, change the Deployment image and remove the `code` ConfigMap
volume and its `/app` volumeMount so the image's bundled source is used.

## Run locally

Requires Python 3.10+ on Linux or macOS; no third-party dependencies.

```bash
DATA_DIR="$PWD/.data" POD_NAME=turtle-local python3 app.py
```

Open <http://localhost:8080>. Stop and restart the process with the same data
directory to see persistence. The code does not change ownership of local files.

## Verification

```bash
python3 -m unittest discover -s tests -v
```

Tests cover a hard process kill and exact state recovery, a second writer being
refused, read-only storage, and committed maze movement / sequence consistency.
The release was exercised as a local Python process, its JavaScript passed a
syntax check, and both YAML variants were parsed and checked against the source.
Visual browser rendering, a container build and deployment on VKS/OpenShift still
need checking in your environment. No browser, Kubernetes cluster or Docker
daemon was available here; downloading a browser was unsuccessful.

## Files

- `app.py`, `index.html`: complete backend and frontend source.
- `k8s/*.yaml`: standalone deployable manifests with source embedded.
- `build_manifests.py`: regenerates those manifests without dependencies.
- `Dockerfile`: optional image packaging.
- `tests/test_persistence.py`: integration tests.

Delete the Deployment to stop the demo while preserving the PVC. Deleting the
namespace/PVC may delete the underlying volume according to its reclaim policy.
