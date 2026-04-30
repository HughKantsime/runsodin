# `odin-demo` namespace — K8s manifests

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

```sh
make -f ops/demo/Makefile.demo demo-k8s-up
```

Which runs:

1. `kubectl apply -f ops/demo/k8s/00-namespace.yaml`
2. `kubectl apply -f ops/demo/k8s/05-secrets.yaml`  (generated, see below)
3. `make demo-k8s-configmaps`  (generates fixture + publisher-script ConfigMaps from in-repo files)
4. `kubectl apply -f ops/demo/k8s/10-mosquitto.yaml`
5. `kubectl apply -f ops/demo/k8s/20-odin-pvc.yaml`
6. `kubectl apply -f ops/demo/k8s/40-odin.yaml`
7. `kubectl apply -f ops/demo/k8s/50-publisher.yaml`
8. `kubectl apply -f ops/demo/k8s/60-ingress.yaml`
9. `kubectl apply -f ops/demo/k8s/70-networkpolicy.yaml`
10. `kubectl apply -f ops/demo/k8s/80-cronjob-reset.yaml`

## Secrets

`05-secrets.yaml` is **gitignored**. Generate freshly with
`make -f ops/demo/Makefile.demo demo-k8s-secrets` — it runs
`python3 -c '...Fernet/secrets...'` to mint:

- `odin-demo-secrets/encryption_key`     (Fernet)
- `odin-demo-secrets/jwt_secret_key`     (32-byte URL-safe)
- `odin-demo-secrets/api_key`            (24-byte URL-safe)
- `demo-reviewer-credentials/email`      (`appreview@demo.subsystem.app`)
- `demo-reviewer-credentials/password`   (16-char alphanumeric)
- `demo-reviewer-credentials/rotated_at` (ISO timestamp)

NEVER reuse prod values. Both secrets stay in-cluster only.

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

`odin-demo-reset` CronJob, daily 03:00 UTC. Wipes `odin-demo-data` PVC,
rotates the reviewer password into the `demo-reviewer-credentials`
Secret, scales `odin` + `publisher` back up. ~60 s wall-clock.
