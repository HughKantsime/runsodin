# Remote access over Tailscale

ODIN can be reached privately from a phone or laptop using the optional `tailscale`
Compose profile. Install Tailscale on each client and sign in to the same tailnet.
ODIN authentication and role/organization permissions still apply. No router port
forwarding is needed. **Do not enable Funnel**: it would make the service reachable
from the public internet. Tailnet grants/ACLs should admit only the intended users
and devices to this node's TCP ports 443 and 8555.

## Topology

The pinned official `tailscale/tailscale:v1.90.9` sidecar uses
its own network namespace on the Compose default network. ODIN continues owning
its published LAN port 8000, including when the profile is disabled. Tailscale
Serve terminates HTTPS on the node's tailnet address at port 443 and proxies to
`http://odin:8000` using Docker service DNS. HTTP, API and WebSocket upgrades use
this same origin. There is no `network_mode: service:odin` or `depends_on` coupling.
Watchtower or Compose can replace ODIN without recreating Tailscale: new backend
connections resolve `odin` again. In-flight connections can drop during an update;
clients reconnect when ODIN is ready.
The sidecar's persistent state is `${ODIN_DATA_PATH}/tailscale`; ODIN never reads it.

Userspace mode is the default: no TUN device or extra capability is needed, and
no 100.x interface appears on the Docker host. Serve handles the inbound tailnet
connections in Tailscale's userspace stack. A raw Serve TCP forward on tailnet
port 8555 forwards to go2rtc on `odin:8555`. go2rtc's API (1984) and RTSP
server (8554) remain loopback-only. There are no new published host ports.

The JSON loaded by `TS_SERVE_CONFIG` supports these non-loopback hostname targets
in **v1.90.9**. Containerboot calls `SetServeConfig` directly; the HTTP backend
parser accepts `http://odin:8000`, and both HTTP and `TCPForward` dial through
`SystemDial` / Go's network dialer, which resolves Docker DNS on new connections.
Although the `TCPForward` field comment says IP:port, this JSON path does not
require an IP literal. The `tailscale serve` CLI target validator has separate
localhost restrictions: keep the supplied JSON instead of recreating it with CLI
target arguments. See the pinned source references below. No static Docker IP or
startup-only IP substitution is used; either would become stale after replacement.
If a future pinned version requires literal TCP targets, use a DNS-aware TCP relay
in the sidecar namespace (Serve to loopback, relay resolving `odin:8555` for each
connection), or regenerate/reload Serve on Docker DNS changes. Recheck this before
updating the pin; a one-time resolved IP is insufficient.

The sidecar is explicitly **excluded from Watchtower** with
`com.centurylinklabs.watchtower.enable: "false"`. Its pinned-version updates are
manual and intentional. ODIN's existing Watchtower policy remains enabled in
production.

The go2rtc version pinned by ODIN is 1.9.4. ODIN's explicit
`webrtc.listen: 0.0.0.0:8555` enables TCP and UDP; an added
`ODIN_TAILNET_IP:8555` candidate lets a browser use ICE-TCP through Serve.
`ODIN_TAILNET_IP` is the **sidecar's tailnet IPv4** (`tailscale ip -4`), never
ODIN's Docker address or the sidecar's bridge address. The advertised address
therefore still lands at Serve after either container is recreated with its saved
Tailscale state. In go2rtc 1.9.4, `AddCandidate` generates both TCP and UDP
candidates when the listen address has no protocol suffix; this retains the
ICE-TCP candidate without changing the LAN behavior. Serve
forwards **raw TCP**, not TLS-terminated TCP or UDP, on 8555. The existing LAN
candidate is retained. Do not replace `ODIN_HOST_IP` with the tailnet address.

This supports a userspace TCP media path in configuration. A real joined tailnet,
cellular client and camera were not available for end-to-end verification. Browser
ICE-TCP and camera codec compatibility remain live acceptance checks. If no video
frame arrives within 12 seconds, the web UI shows authenticated snapshots refreshed
every five seconds, with an explanation. It resumes video if frames start arriving.
This fallback also covers missing tailnet candidates and blocked media ports. Native
iOS camera fallback behavior is outside this repository and has not been verified.

## Setup

1. Use an ODIN image containing this change and Docker Compose **2.23.1 or newer**
   (the installer Compose uses inline `configs.content`). Keep the installer
   `docker-compose.yml` in your installation directory.
2. Install the Tailscale app on the phone and laptop. Join the owner's tailnet.
   In the Tailscale admin console, enable **MagicDNS** and **HTTPS Certificates**
   under DNS. Certificate names appear in public certificate-transparency logs;
   this does not expose the service publicly.
3. The installation operator adds this block to the private `.env`, replacing placeholders
   (the production workflow below uses interactive login and no auth key):

   ```dotenv
   TS_HOSTNAME=odin
   TS_USERSPACE=true
   ODIN_TRUSTED_PROXIES=127.0.0.1,::1,tailscale
   TS_AUTHKEY=<owner-supplied-auth-key>
   ODIN_PUBLIC_URL=https://odin.<tailnet>.ts.net
   ODIN_TAILNET_IP=
   # Keep the existing LAN address:
   ODIN_HOST_IP=<server-LAN-IP>
   # For installations that use browser login over plain LAN HTTP:
   COOKIE_SECURE=false
   ```

   The key is supplied only to the Tailscale container environment. Never commit
   it or paste it in logs, support bundles or command arguments. Protect `.env`
   with mode 600. `TS_AUTH_ONCE=true` reuses the saved identity after enrollment;
   remove the key from `.env` after joining and recreate the sidecar. Never share
   its state directory. Diagnostics/status endpoints do not read its key or state.
4. Start the profile:

   ```sh
   docker compose --profile tailscale up -d
   ```

   Alternatively leave `TS_AUTHKEY` empty, start the profile, then run
   `docker compose exec tailscale tailscale up` and complete the displayed browser
   login yourself. The container may wait for authentication before applying Serve.
5. Obtain the exact node name from the Tailscale admin console (a name collision
   can add a suffix). Set `ODIN_PUBLIC_URL` to that full HTTPS name. Get the
   candidate using `docker compose exec tailscale tailscale ip -4` and put that
   address into `ODIN_TAILNET_IP`. Apply these ODIN settings without touching the sidecar:

   ```sh
   docker compose --profile tailscale up -d --no-deps --force-recreate odin
   ```

   Changing either environment setting requires recreation; go2rtc's startup
   config then includes the candidate. Retain the Tailscale state directory to
   retain the same node identity and address. Subsequent ODIN replacements do not
   require any sidecar action. When disabling remote access, remove `tailscale`
   from `ODIN_TRUSTED_PROXIES` (or unset it) and recreate ODIN. Compose profiles
   select services, not environment values; the explicit proxy setting accompanies
   enabling the profile. Without that setting, the default remains loopback-only.
6. Open `https://odin.<tailnet>.ts.net` from a joined client and sign in to ODIN.
   In Settings → General → Network & Camera Streaming, **Remote access** shows
   the iOS server URL and tests `/health` from that browser. Enter that exact
   HTTPS URL (no port 8000 or `/api` suffix) into the iOS app. The check uses no
   credentials and is not an outbound request from the ODIN server; a disconnected
   browser cannot verify a tailnet URL.

`ODIN_PUBLIC_URL` accepts only an HTTPS origin, without userinfo, path, query or
fragment. It is added to trusted hosts and credentialed CORS origins. With an
explicit `TRUSTED_HOSTS` list, include the ts.net hostname if you do not use
`ODIN_PUBLIC_URL`; preserve LAN hosts and localhost for health checks. When checking
a remote URL from another origin, add that browser's exact origin to `CORS_ORIGINS`
as well. Wildcard credentialed CORS is not accepted.

## Cookies, URLs and network policies

The app trusts `X-Forwarded-Proto: https` only from explicit socket peers in
`ODIN_TRUSTED_PROXIES` (defaults `127.0.0.1,::1`). For this profile set
`127.0.0.1,::1,tailscale`: Serve connects from its Docker bridge address, and ODIN
resolves the `tailscale` service hostname to IPv4/IPv6 addresses. Positive and
negative DNS results are cached for five seconds per app worker. The next
forwarded request after expiry triggers re-resolution with a two-second lookup
timeout; concurrent requests share that refresh. Failed/empty/timed-out resolution
trusts no addresses for that hostname, and expired answers are discarded. Other
explicit proxies remain independent. A recreated sidecar's new bridge IP is
recognized after the cache expires; the old address can remain trusted only until
expiry. DNS and control of service aliases on this Compose network are therefore
part of the trust boundary. Only trusted operators may attach containers using
the `tailscale` alias. Do not use public or untrusted DNS names as proxies.

Uvicorn runs with `--no-proxy-headers`, so arbitrary forwarded headers
cannot change the peer before ODIN checks it. A custom ASGI launch must use that
flag too. Never configure a wildcard or a whole LAN as trusted proxies.

The expected Serve headers are the original `Host`, `X-Forwarded-Proto: https`,
and `X-Forwarded-For` with the connecting client's IP appended on the right.
**Assumption to verify live against the pinned Serve version:** verify these
headers and the restored client identity on a real HTTPS and WebSocket request;
do not record header values in diagnostics. ODIN ignores forwarded Host and uses
only the right-most XFF entry from a trusted socket peer, including when multiple
XFF fields are present. It accepts valid IPv4/IPv6 literals, without requiring a
Tailscale range (normally `100.64.0.0/10` or `fd7a:115c:a1e0::/48`). Proxy identity
headers never log users in.

HTTPS always gets Secure session cookies, including password, MFA and OIDC
sessions, even if `COOKIE_SECURE=false` is needed for LAN HTTP. Plain HTTP retains
its prior `COOKIE_SECURE` policy: default true, false for working HTTP browser
sessions. Cookie policy is per request, not a global switch based on the public URL.
OIDC binding cookies also upgrade on verified HTTPS; the configured SameSite
policy is retained. Use `COOKIE_SAMESITE=lax` for cross-origin OIDC where needed.

Checked absolute URL producers:

- OIDC: explicit `OIDC_REDIRECT_URI` wins; otherwise request origin (including
  trusted HTTPS scheme) supplies the callback. Register each intended callback
  with the IdP. A fixed callback to a different origin from the login page loses
  the host-bound binding cookie; start SSO on the callback's origin.
- Password-reset email links: `core.application_url.configured_base_url` prefers
  `ODIN_PUBLIC_URL`, otherwise the existing Notifications base URL. Ordinary
  alert email bodies do not generate an ODIN absolute URL; push navigation uses
  relative `/alerts` links.
- Native iOS: the server URL comes from the app's configured connection; this
  backend does not generate a separate iOS deep-link base. No native code changed.
- OBS overlay copy links in `PrinterCard` use `window.location.origin`; the overlay
  camera uses a relative authenticated MJPEG API URL. Copy from the tailnet UI
  for remote OBS, and keep its camera-only token private.
- WebSocket and camera signaling, snapshot and preview routes use the current
  browser origin or relative paths.

IP allowlisting (including the allowlist-save lockout check), per-IP rate limits,
login lockouts, audit IPs and session records use the restored client address.
Include the intended tailnet clients or CIDRs in an enabled allowlist. Tailnet
ACLs and ODIN RBAC still apply. Trusted forwarding with a missing or malformed
right-most client IP receives the fixed non-loopback, non-private sentinel
`192.0.0.9` (never contacted); an enabled IP allowlist always denies this sentinel,
even with a catch-all CIDR. ODIN logs one warning per middleware instance without
header values. Forwarded loopback, unspecified, scoped and IPv4-mapped IPv6
addresses are rejected. Loopback requests without forwarding headers retain their identity
for go2rtc, internal relay traffic and health checks. `/internal/camera-source`
refuses forwarded tailnet requests, including HTTPS requests missing XFF.
Loopback processes and the resolved sidecar addresses are inside the proxy trust boundary.

## Proxmox LXC and kernel mode

Use userspace mode when `/dev/net/tun` is absent. Nothing on the Proxmox host needs
to change for the default profile. Kernel mode is optional and requires the host administrator
to pass the TUN device into the LXC on the Proxmox host, permit device major/minor
10:200, and ensure Docker can use it. The exact LXC configuration is host-policy
specific; the host administrator applies it.

Then add this `tailscale-kernel.yml` override and set `TS_USERSPACE=false`:

```yaml
services:
  tailscale:
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
```

```sh
docker compose -f docker-compose.yml -f tailscale-kernel.yml --profile tailscale up -d --no-deps --force-recreate tailscale
```

The topology and Serve config stay the same. The interface is in the sidecar's
namespace, not ODIN's or the host's. ODIN's listeners are reached through the
bridge proxies; restrict node ACLs to 443/8555. Preserve ODIN's loopback bindings
for 1984/8554. Kernel mode on a real LXC remains a live validation item.

## Existing production Compose: applied by admin tooling (Claude)

After the image containing this change is released, the operator's admin tooling
(Claude) applies this production change in `/opt/odin/runsodin`, starts the
sidecar and runs `tailscale up` interactively. **The owner's only setup step is
approving the displayed Tailscale login URL.** Claude and Codex handle no auth key.
No production changes were made during this implementation.

The exact Compose addition is `/opt/odin/runsodin/tailscale-remote.yml` below;
`/opt/odin/runsodin/docker-compose.yml` has no edits. Both files are used in every
command below. The override explicitly retains production's
`ghcr.io/hughkantsime/odin:latest`, `container_name: odin`, and published ports
8000, 1984 and 8555, and its Watchtower enable label. The sidecar has an explicit
Watchtower disable label. Compose merges the existing volumes and other environment
settings. Do not replace the production Compose file with the installer template.

```diff
--- /dev/null
+++ /opt/odin/runsodin/tailscale-remote.yml
@@ -0,0 +1,35 @@
+services:
+  odin:
+    image: ghcr.io/hughkantsime/odin:latest
+    container_name: odin
+    labels:
+      com.centurylinklabs.watchtower.enable: "true"
+    ports:
+      - "8000:8000"
+      - "1984:1984"
+      - "8555:8555"
+    environment:
+      ODIN_PUBLIC_URL: ${ODIN_PUBLIC_URL:-}
+      ODIN_TAILNET_IP: ${ODIN_TAILNET_IP:-}
+      ODIN_TRUSTED_PROXIES: ${ODIN_TRUSTED_PROXIES:-127.0.0.1,::1}
+  tailscale:
+    image: tailscale/tailscale:v1.90.9
+    profiles: [tailscale]
+    restart: unless-stopped
+    labels:
+      com.centurylinklabs.watchtower.enable: "false"
+    environment:
+      TS_AUTH_ONCE: "true"
+      TS_HOSTNAME: ${TS_HOSTNAME:-odin}
+      TS_USERSPACE: ${TS_USERSPACE:-true}
+      TS_STATE_DIR: /var/lib/tailscale
+      TS_SERVE_CONFIG: /config/serve.json
+    volumes:
+      - ${ODIN_DATA_PATH:-./odin-data}/tailscale:/var/lib/tailscale
+    configs:
+      - source: tailscale_serve
+        target: /config/serve.json
+configs:
+  tailscale_serve:
+    content: |
+      {"TCP":{"443":{"HTTPS":true},"8555":{"TCPForward":"odin:8555"}},"Web":{"$${TS_CERT_DOMAIN}:443":{"Handlers":{"/":{"Proxy":"http://odin:8000"}}}},"AllowFunnel":{}}
```

The exact additions to `/opt/odin/runsodin/.env` are below. Claude substitutes the
actual full MagicDNS name and IPv4 returned by the sidecar after login; placeholders
are never used as the final values. Existing LAN, cookie and secret settings stay
in place. No `TS_AUTHKEY` is added or passed to this production sidecar.

```diff
--- /opt/odin/runsodin/.env
+++ /opt/odin/runsodin/.env
@@ -0,0 +1,5 @@
+TS_HOSTNAME=odin
+TS_USERSPACE=true
+ODIN_TRUSTED_PROXIES=127.0.0.1,::1,tailscale
+ODIN_PUBLIC_URL=https://<actual-node>.<actual-tailnet>.ts.net
+ODIN_TAILNET_IP=<tailscale-ip-v4>
```

For initial enrollment Claude starts only the sidecar (ODIN is already running),
before adding the final URL/IP values above, and presents the login URL to the owner:

```sh
cd /opt/odin/runsodin
docker compose -f docker-compose.yml -f tailscale-remote.yml --profile tailscale config -q
docker compose -f docker-compose.yml -f tailscale-remote.yml --profile tailscale up -d --no-deps tailscale
docker compose -f docker-compose.yml -f tailscale-remote.yml exec tailscale tailscale up
```

After the owner approves the URL, Claude obtains the exact node name from Tailscale
status and the candidate from `tailscale ip -4`, applies the `.env` additions, and
recreates only ODIN:

```sh
docker compose -f docker-compose.yml -f tailscale-remote.yml exec tailscale tailscale ip -4
docker compose -f docker-compose.yml -f tailscale-remote.yml --profile tailscale config -q
docker compose -f docker-compose.yml -f tailscale-remote.yml --profile tailscale up -d --no-deps --force-recreate odin
```

The default state path is `/opt/odin/runsodin/odin-data/tailscale`; an existing
`ODIN_DATA_PATH` overrides it. Claude retains this directory across recreations.
Keep the override in future Compose commands so remote settings remain applied.
Watchtower can continue replacing `odin` independently with the existing enable
label; Tailscale stays running on the default network. To upgrade Tailscale,
review/change its pinned version and intentionally recreate only that service.
Use `config -q` so diagnostics do not print existing environment secrets.
The production 1984 mapping is retained exactly; no port cleanup is included.

## Troubleshooting and live acceptance

- **No HTTPS:** confirm the client is connected, the exact MagicDNS name, HTTPS
  Certificates enabled, node approval/expiry and ACL access to 443. Use
  `docker compose exec tailscale tailscale serve status` to check Serve; never
  attach state files or auth keys to a support report.
- **Invalid Host:** check `TRUSTED_HOSTS` and `ODIN_PUBLIC_URL`; recreate ODIN
  after an ODIN environment change. LAN addresses must remain allowed too.
- **Login loop:** HTTPS must produce a Secure session cookie. LAN HTTP needs the
  same `COOKIE_SECURE=false` configuration it needed before. Check launch flags
  and proxy peer allowlist; do not trust arbitrary forwarded headers to fix it.
- **Snapshots instead of video:** confirm `ODIN_TAILNET_IP` matches `tailscale ip -4`,
  ACLs allow TCP 8555, and Serve shows the raw 8555 forward. UDP on a userspace
  tailnet candidate will not work; ICE must select TCP. Some clients/codecs may
  need the snapshot fallback. Snapshots require a functioning go2rtc camera source
  and JPEG conversion (the fallback uses ODIN’s existing MJPEG variant); an offline camera displays a clear unavailable message.
- **Health check fails but the remote UI works:** cross-origin checks need the
  viewing origin in CORS. The check is from the viewing browser, not the server.
- **After an update:** ODIN downtime interrupts existing connections; new HTTPS,
  WebSocket and media connections must recover without restarting Tailscale. Check
  both services remain on the same Compose default network and that Docker DNS
  resolves `odin` from the sidecar and `tailscale` from ODIN. After a sidecar IP
  change, allow five seconds for proxy-trust refresh. Repeated DNS failure denies
  forwarding trust; do not expand trust to the entire Docker subnet.

After Claude enrolls the server with the owner’s login approval, validate from a phone
on cellular with Tailscale enabled:
verify iOS login, web login, live WebSocket updates and video or the documented
snapshot fallback. Then verify LAN login at the existing HTTP address (for the
referenced production installation, `http://192.168.71.211:8000`). No production
host was contacted during implementation. These checks cannot be replaced by
unit tests or Compose validation. Also record the sidecar container ID/start time,
force-recreate **only ODIN**, and verify fresh HTTPS requests, WebSocket reconnects
and ICE-TCP media with that same sidecar still running. Repeat with a Watchtower
ODIN image update. Separately recreate only the sidecar retaining its state;
verify trusted HTTPS/client identity recovers after five seconds even if its bridge
IP changes. Do not claim uninterrupted in-flight connections during replacements.

## Upstream references and remaining assumptions

- [Tailscale userspace networking](https://tailscale.com/docs/concepts/userspace-networking)
- [Docker parameters and TS_SERVE_CONFIG](https://tailscale.com/docs/features/containers/docker/docker-params)
- [Serve HTTPS and raw TCP forwarding](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [Pinned Serve implementation](https://github.com/tailscale/tailscale/blob/v1.90.9/ipn/ipnlocal/serve.go)
- [Pinned Serve JSON types and separate CLI target restrictions](https://github.com/tailscale/tailscale/blob/v1.90.9/ipn/serve.go)
- [Pinned system dialer](https://github.com/tailscale/tailscale/blob/v1.90.9/net/tsdial/tsdial.go)
- [Pinned container config substitution](https://github.com/tailscale/tailscale/blob/v1.90.9/cmd/containerboot/serve.go)
- [go2rtc WebRTC options](https://github.com/AlexxIT/go2rtc/blob/master/internal/webrtc/README.md)
- [Pinned go2rtc candidate expansion](https://github.com/AlexxIT/go2rtc/blob/v1.9.4/internal/webrtc/candidates.go)
- [ODIN's pinned go2rtc SDP/candidate handling](https://github.com/AlexxIT/go2rtc/blob/v1.9.4/internal/webrtc/webrtc.go)

The TCP media path is inferred from these implementations; no claim of successful
cellular streaming is made. Certificate issuance, tailnet enrollment, tag/grant
policy, native iOS acceptance and LXC kernel-mode behavior require live
verification coordinated by the operator. The pinned Tailscale source tag was
verified; pulling the Docker image and running a joined container were not part of this work.

## Topology revision validation (2026-10-27)

From the repository root, this revision passed **114 tests** with Python 3.11:

```sh
env -i PATH="$PATH" HOME=/private/tmp python3.11 -m pytest \
  tests/test_contracts/test_remote_access.py \
  tests/test_contracts/test_remote_proxy_identity.py \
  tests/test_contracts/test_ip_allowlist_behavior.py \
  tests/test_contracts/test_ip_allowlist_cache.py \
  tests/test_contracts/test_integ_oidc_binding_cookie.py \
  tests/test_contracts/test_integ_enterprise_go2rtc.py \
  tests/test_contracts/test_integ_go2rtc_prestart.py \
  tests/test_contracts/test_integ_compose_go2rtc_ports.py -q --tb=short
```

New coverage includes IPv4/IPv6 hostname resolution, concurrent cache refresh,
cache reuse, replacement IP adoption and old-IP revocation, initial DNS failure,
empty answers, timeout, failed refresh without stale trust, recovery after failure,
invalid proxy configuration, mapped-client rejection, and camera-relay refusal
from a trusted bridge peer. Existing identity, cookies, allowlist, OIDC and camera
contracts also passed. Three dependency deprecation warnings remain.

Compose 5.1.3 validated the installer, root Compose, and the documented override
merged with a synthetic production base, each with and without the profile
(**six configurations**). Checks asserted default-network membership, forward
targets, loopback-only defaults, explicit profile proxy trust, and Watchtower labels.
Validation used an isolated Docker config, sanitized environment and
`--env-file /dev/null`; no owner secrets or env files were read. The production
fixture retained its image, container name, ports, LAN/cookie settings and volume.
All touched Python modules are under 700 lines. No commit or production change
was made. Live replacement/ICE-TCP acceptance and Claude's independent review
remain pending; source inspection and contract tests do not substitute for them.

## Earlier implementation validation (before topology revision, 2026-10-27)

From the repository root, the review-fix backend run passed **495 tests**:

```sh
python3.11 -m pytest \
  tests/test_contracts/test_remote_access.py \
  tests/test_contracts/test_remote_proxy_identity.py \
  tests/test_contracts/test_ip_allowlist_behavior.py \
  tests/test_contracts/test_ip_allowlist_cache.py \
  tests/test_contracts/test_integ_oidc_binding_cookie.py \
  tests/test_contracts/test_integ_enterprise_go2rtc.py \
  tests/test_contracts/test_integ_go2rtc_prestart.py \
  tests/test_contracts/test_integ_compose_go2rtc_ports.py \
  tests/test_contracts/test_route_auth_census.py \
  tests/test_contracts/test_claims_backend_rbac_census.py \
  tests/test_contracts/test_rbac_matrix_completeness.py \
  tests/test_contracts/test_rbac_matrix_enforcement.py \
  tests/test_contracts/test_auth_transport_claim_acceptance.py \
  tests/test_contracts/test_mfa_authentication_behavior.py \
  tests/test_contracts/test_session_issuance_behavior.py \
  tests/test_contracts/test_middleware_auth.py \
  tests/test_contracts/test_cert_B_camera_fixes.py \
  tests/test_contracts/test_cert_A_camera_sources.py \
  tests/test_contracts/test_cert_F_C_base_url_upgrade.py -q --tb=short
```

The new regressions first reproduced the loopback bypass, then passed after the
fix. They cover right-most XFF (including repeated headers), IPv4/IPv6, spoofing,
malformed/missing identity, log-once redaction, internal loopback preservation,
relay refusal, allowlist enforcement/save checks, separate client login throttling,
and audit/session IPs. A final focused rerun passed **43 tests** after the production
patch documentation changed; these overlap the 495-test run.

`tests/test_rbac.py` executed from the repository root using `python3.11 -m pytest`
and a temporary in-process transport adapter: **1,851 passed, 9 existing skips**.
The sandbox refuses loopback socket binds, so the adapter used the cert-G isolated
SQLite/app factory and synthetic Enterprise license, routed requests through the
real ASGI middleware/handlers, and closed SSE after response headers as the live
fixture does. It changed no RBAC assertions and contacted no existing server.
This is an in-process matrix pass, not a live HTTP-server or tailnet pass.

The dotenv-skipping config/test additions and both release-gate environment
allowlist entries were reverted; no committed test required them. No existing
secret or `.env` file was read. The separate frontend/build and release-precheck
evidence below is from the preceding implementation, not rerun for this backend
review fix: frontend camera, hook and Settings checks passed **99 tests** across
14 files, and `npm --prefix frontend run build` passed. Typechecking reported
**289 existing errors**, identical on baseline and changed trees.

Both Compose profile configurations validated using a clean environment and
`--env-file /dev/null`; no owner environment was loaded. The documented production
override also validated against a synthetic copy of the supplied production
image/container/ports shape and retained its LAN, cookie and volume settings.
The SQLite inventory was
refreshed and passed (422 sites), `git diff --check` passed, and all new/modified
Python modules are at most 700 lines.

During the preceding implementation, `make release-precheck` was attempted but could not install its pinned dependencies
because PyPI DNS/network access was blocked. An additional live auth suite had
11 setup errors because the sandbox forbids binding localhost sockets. No live
browser, tailnet, camera or native-device pass is claimed. Claude's independent
review is still required before release. No production contact or commit occurred.
