# `odin-demo` namespace — K8s EDU sandbox manifests

Mirrors `ops/demo/docker-compose.demo.yml` but on the M4 K3s cluster
behind the existing CF tunnel + cert-manager + Traefik stack.

## Why K3s (vs PVE LXC)

Decision driver: pattern-match-to-existing-infra wins on speed and
operability for an App Review reviewer endpoint.

| Aspect                  | K3s (chosen)                                              | PVE LXC                                       |
| ----------------------- | --------------------------------------------------------- | --------------------------------------------- |
| Cert mgmt               | cert-manager + DNS-01 + Traefik (already in use)          | Caddy/Traefik on-host (mirror prod LXC 112)   |
| Network isolation       | Namespace + NetworkPolicy + default-deny-all              | Separate LXC + subnet rules                   |
| Reset cron              | k8s CronJob in-cluster                                    | systemd timer on the LXC                      |
| Pattern match           | 7+ existing public hostnames already use this exact stack | ODIN prod is this pattern, demo would be twin |
| Blast radius            | Cluster-shared kernel, isolated namespace + NetPol        | LXC-shared kernel, separate LXC               |
| Add to monitoring       | Already wired (kube-state-metrics, Traefik dashboard)     | Extra Prometheus target needed                |

K3s also lets us reuse the cert-manager + ACME DNS-01 setup that the
existing CF API token (zone-edit scope) already powers — no new
account-level CF perms required for cert provisioning.

## Apply order

Provision secrets and the signed license before applying the manifests:

```sh
make -f ops/demo/Makefile.demo demo-k8s-secrets
kubectl apply -f ops/demo/k8s/00-namespace.yaml
kubectl apply -f ops/demo/k8s/05-secrets.yaml
make -f ops/demo/Makefile.demo demo-k8s-license \
  ODIN_DEMO_LICENSE_FILE=/secure/path/odin.license
make -f ops/demo/Makefile.demo demo-k8s-up
```

The final target runs:

1. `kubectl apply -f ops/demo/k8s/00-namespace.yaml`
2. `kubectl apply -f ops/demo/k8s/05-secrets.yaml`  (generated, see below)
3. Verify that the previously provisioned `odin-demo-license` Secret exists.
4. Generate fixture + publisher-script ConfigMaps from in-repo files.
5. Apply mosquitto, PVC, ODIN, publisher, ingress, NetworkPolicy, and reset
   CronJob manifests in order.

`demo-k8s-up` refuses to deploy ODIN until both the generated credential
manifest and `odin-demo-license` Secret exist. It does not generate or accept a
license-signing private key.

`demo-k8s-license` reads the live sandbox's `/data/.odin-install-id` and rejects
a signed artifact whose binding does not match. The base demo must therefore
be running once before its Education license is issued and installed.

## Secrets

`05-secrets.yaml` is **gitignored**. Generate freshly with
`make -f ops/demo/Makefile.demo demo-k8s-secrets` — it runs
`python3 -c '...Fernet/secrets...'` to mint:

- `odin-demo-secrets/encryption_key`     (Fernet)
- `odin-demo-secrets/jwt_secret_key`     (32-byte URL-safe)
- `odin-demo-secrets/api_key`            (24-byte URL-safe)
- `demo-reviewer-credentials/email`      (`appreview@demo.subsystem.app`)
- `demo-reviewer-credentials/password`   (16-char alphanumeric)
- `demo-reviewer-credentials/edu_admin_email` / `edu_admin_password`
- `demo-reviewer-credentials/edu_teacher_email` / `edu_teacher_password`
- `demo-reviewer-credentials/edu_student_email` / `edu_student_password`
- `demo-reviewer-credentials/rotated_at` (ISO timestamp)

The generator reports only the output filename; it withholds every credential
value. Retrieve credentials from the cluster Secret through the approved
operator channel. Never reuse production values or commit the generated file.

The separate `odin-demo-license` Secret contains only the public signed license
artifact. It is mounted at `/data/odin.license` with `subPath` and read-only,
while the rest of `/data` remains writable.

## Network surface

- **Public hostname**: `https://demo.subsystem.app`
- **CNAME**: `demo` → `21c8f886-ec07-4351-ad3c-519c08d34a80.cfargotunnel.com` (proxied)
- **Tunnel route**: must be added by Hugh in CF dashboard once: see
  `ops/demo/k8s/CF_TUNNEL_ROUTE.md`. Until that's added, the public
  hostname returns CF 404 because the tunnel does not have a hostname
  rule for `demo.subsystem.app`.
- **Ingress backend**: `odin.odin-demo.svc.cluster.local:8000`
- **Internal verification (M4)**:
  ```sh
  curl -sI -H 'Host: demo.subsystem.app' http://192.168.68.201/health
  ```

## Reset cron

`odin-demo-reset` CronJob runs daily at 03:00 UTC. It scales ODIN and the
publisher down, wipes only the mutable data and heartbeat PVCs, then scales
both deployments back up. The signed license and credential Secrets are not
modified, and `.odin-install-id` plus `.odin-device.key` survive the mutable
data wipe. ODIN's startup hook reseeds every configured persona after database
migrations, and startup fails if seeding cannot complete.

The publisher has heartbeat-only startup/readiness/liveness probes with a
90-second freshness threshold. A running Python process whose replay loop is
stalled therefore stops reporting ready and is eventually restarted.
