# Installing on Kubernetes

*[Español](../instalacion-kubernetes.md) · English*

`deploy/kubernetes/amularr.yaml` holds a complete example: a Secret with the
EC password, a 100 Mi PVC for the state file, the Deployment and a
ClusterIP Service. It assumes aMule already runs somewhere reachable (a NAS,
another namespace) and that its Incoming directory is available as a
volume.

## 1. Adjust the manifest

```bash
cp deploy/kubernetes/amularr.yaml amularr.yaml
```

Edit:

- `metadata.namespace` on every object (the example uses `media`).
- `AMULE_EC_HOST` / `AMULE_EC_PORT`: where aMule listens for External
  Connections. In-cluster aMule: its Service name. NAS: its IP.
- `ec_password_md5` in the Secret: the `Password=` line of aMule's
  `remote.conf`, or the output of `echo -n 'password' | md5sum`. To use the
  clear-text password instead, set `AMULE_EC_PASSWORD` and drop the MD5 env.
- The `amule-incoming` volume: whatever gives the pod aMule's Incoming
  directory (NFS export, hostPath, RWX PVC). Mount the same thing at the same
  path in the Sonarr and Radarr pods, or configure a remote path mapping in
  them.
- `securityContext`: uid/gid matching the *arr pods, so files amularr deletes
  after an import are its own.
- The PVC's `storageClassName` if your cluster has no default class.

Optional: `sonarr_api_key` / `radarr_api_key` in the Secret enable the
wanted-list searches (see [usage.md](usage.md)). They are read with
`optional: true`, so the pod starts without them.

## 2. Apply

```bash
kubectl apply -f amularr.yaml
kubectl -n media rollout status deploy/amularr
kubectl -n media logs deploy/amularr
```

The log ends with `listening on 0.0.0.0:8080` and, when the keys are set,
`wanted-list searches enabled for: tv, movies`.

## 3. Verify from inside the cluster

```bash
kubectl -n media run curl --rm -it --image=curlimages/curl --restart=Never -- \
  sh -c 'curl -s http://amularr:8080/health; echo; curl -s "http://amularr:8080/api?t=caps" | head -c 300'
```

Point Prowlarr at `http://amularr.<namespace>.svc:8080` and the Sonarr/Radarr
download client at host `amularr.<namespace>.svc`, port `8080`
([usage.md](usage.md)).

## Secrets managed elsewhere

With External Secrets Operator (or sealed-secrets, SOPS, ...), replace the
plain Secret with your own object that produces a Secret named `amularr`
with keys `ec_password_md5`, `sonarr_api_key` and `radarr_api_key`. Nothing
else in the manifest changes.

## Upgrading

Change the image tag and apply again. The Deployment uses the `Recreate`
strategy because two replicas would fight over the state file; a rollout
takes a few seconds and in-flight downloads keep going in aMule meanwhile.

## Probes

- `/ping` never touches aMule and backs the liveness probe.
- `/health` connects to aMule and answers 503 while the EC link is down; it
  backs the readiness probe only, so a NAS or aMule restart takes amularr
  out of the Service without restarting the pod in a loop.
