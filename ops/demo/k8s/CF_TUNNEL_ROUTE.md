# Cloudflare tunnel public hostname route — `demo.subsystem.app`

## What's already wired

- DNS CNAME `demo.subsystem.app` → `21c8f886-ec07-4351-ad3c-519c08d34a80.cfargotunnel.com` (proxied) — written via API with the existing zone-edit token.
- K3s ingress `odin-demo/odin-demo` is bound to host `demo.subsystem.app` and serves the demo backend at `odin.odin-demo.svc.cluster.local:8000`.
- Cert-manager issued the LE leaf for `demo.subsystem.app` via DNS-01 (cert `demo-subsystem-app-tls/Ready=True`).

## What's NOT wired (the only remaining blocker)

The `subsystem.app` Cloudflare Tunnel (`21c8f886-ec07-4351-ad3c-519c08d34a80`) is **token-managed** (running with `--token`, ingress rules live in the CF dashboard). Adding a new public hostname to a token-managed tunnel requires a CF API token with **`Account: Cloudflare Tunnel:Edit`**, which the existing `cert-manager/cloudflare-api-token` does NOT have (it only has `Zone: DNS:Edit` for ACME DNS-01).

Without the public hostname rule, the tunnel returns CF 404 for `demo.subsystem.app` even though DNS resolves and the K3s backend is healthy.

## How to add the route — pick one (Hugh)

### Option A — Cloudflare dashboard (≈30 s)

1. https://one.dash.cloudflare.com → Networks → Tunnels → click the `subsystem.app` tunnel.
2. **Public Hostname** tab → **Add a public hostname**.
3. Fill in:
   - Subdomain: `demo`
   - Domain: `subsystem.app`
   - Service: Type `HTTP`, URL `traefik.kube-system.svc.cluster.local:80`
4. (Optional but recommended) Open **Additional application settings → HTTP Settings** and set `HTTP Host Header = demo.subsystem.app` so traefik routes by host. This already happens implicitly if "Origin Server Name" is left blank — leave default.
5. **Save**.

That single click resolves the blocker and the public probe in `ops/demo/Makefile.demo demo-probe-public` will go green.

### Option B — Mint a tunnel-scoped API token + I'll write the route myself

Create a new CF API token at https://dash.cloudflare.com/profile/api-tokens with these permissions:

- Account → `Cloudflare Tunnel: Edit`

Token TTL: 1 day, IP-restricted to your home/M4 if you like.

Drop it into K3s as a separate secret so I can pick it up:

```sh
kubectl create secret generic cloudflare-tunnel-token -n cloudflare \
  --from-literal=api-token='<paste here>'
```

Then comment back on ODIN-129 and I'll run a single API call to add the public hostname:

```sh
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/d67e8ae9adaa61d8e758197a83d96656/cfd_tunnel/21c8f886-ec07-4351-ad3c-519c08d34a80/configurations" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config":{"ingress":[{"hostname":"demo.subsystem.app","service":"http://traefik.kube-system.svc.cluster.local:80","originRequest":{"httpHostHeader":"demo.subsystem.app"}},{"service":"http_status:404"}]}}'
```

⚠️ Option B requires merging with the existing dashboard-defined rules so we don't drop the other 7+ public hostnames. If the cluster's existing tunnel config can be enumerated via the same token (`GET …/configurations`), I'll merge in-place. If not, Option A is the safer move.

## Recommendation

**Option A.** It's a 30-second click in the dashboard you're already logged into; no token rotation, no risk of merging-over the other public hostnames.
