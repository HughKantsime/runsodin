# ITAR mode

`ODIN_ITAR_MODE=1` is an outbound network policy. It supports air-gapped and CMMC-style deployments. It is not a certification, and no part of O.D.I.N. is a CMMC, FedRAMP or ITAR certification. See [CONTROLLED_PARTS.md](CONTROLLED_PARTS.md) for the separate controlled-part labels.

It is set in the container environment, not in the UI or the database, so a compromised admin account cannot turn it off.

Accepted values: `1`, `true`, `yes` or `on` turn it on; `0`, `false`, `no`, `off`, an empty value or no value leave it off. Letter case and spaces around the value do not matter. Any other value (for example `"1"` with the quotes included, or `2`) stops the container at start with a message that lists the accepted values, so a typing mistake can never leave ITAR mode half on.

## The promise

With ITAR mode on, the server opens no connection to an address outside the local network. Every guarded connection goes to the same address that was checked: a host name is looked up once, the answer is checked, and the connection is made to that answer. Redirects to another address are not followed, and proxy settings from the environment are ignored. The exceptions are listed on this page: an identity provider you allow by name, the PostgreSQL connection (checked, not pinned), and printer discovery (a local-link multicast query).

## What it blocks

- At boot, O.D.I.N. refuses to start if a configured outbound destination (license server, Spoolman, webhooks, SMTP, MQTT republish, an enabled SSO discovery URL) resolves to a public address.
- At run time, every outbound request is checked again and its DNS answer is pinned for the connection. A public answer, or a mix of private and public answers, is refused.
- Webhooks: only a webhook on your local network (10.x, 172.16-31.x, 192.168.x, or IPv6 unique-local) is sent, to the address that was checked. Loopback and link-local addresses and O.D.I.N.'s own ports are refused. The built-in Telegram, Pushover and WhatsApp senders are public services and stay off.
- Mail (SMTP) and MQTT (event republishing, its connection test, smart-plug commands): the server's name is looked up once and the connection, and every MQTT reconnect, goes to that checked address. TLS still uses the configured host name.
- Printers, cameras and smart plugs outside the local network are not contacted. An Elegoo printer cannot redirect its WebSocket connection to another address.
- Cameras (go2rtc): if the camera configuration cannot be written when the container starts, go2rtc starts with no cameras instead of the previous file; if the previous file cannot be replaced, the container does not start. `GO2RTC_CONFIG` must be unset or name the file go2rtc is started with (`/app/go2rtc/go2rtc.yaml`); any other path stops the container at start, because O.D.I.N. would check one file while go2rtc loaded another. When go2rtc runs as its own container (the enterprise layout), O.D.I.N. does not start or stop it, so each time the API container starts it also asks the running go2rtc for its streams and removes every one that is not in the configuration it has just checked; if it cannot do that (go2rtc answers with an error or not in time), the API container does not start. A go2rtc that is not running holds no stream and does not block the start.
- Environment proxy settings are ignored, so identity-provider traffic needs a direct route. `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and every other variable whose name ends in `_PROXY` (either case) are removed from the environment of the container and of every O.D.I.N. process, and `NO_PROXY` is set to `*`. Start-up logs the names that were set and ignored, never their values. A go2rtc that runs as its own container (the enterprise layout) is not started by O.D.I.N.: do not give that container proxy settings.
- Push (APNs) and the hardcoded third-party notification APIs stay off.

Private (RFC 1918, loopback, link-local) destinations are unaffected.

## The PostgreSQL database

With a PostgreSQL database (the enterprise layout), the database server is a connection like any other. With ITAR mode on, O.D.I.N. refuses to start, and does not start `pg_dump` or `pg_restore`, unless the server is on the local network:

- Checked: the host in `DATABASE_URL` and `DATABASE_MAINTENANCE_URL`, the `host` and `hostaddr` settings in the URL query, and the `PGHOST` and `PGHOSTADDR` environment variables. A name must resolve, and every address it has must be private.
- A libpq service file (`service=` in the URL, or `PGSERVICE`) is refused, because the address it names is in a file O.D.I.N. does not read. Put the host in the URL.
- A Unix socket, `localhost`, and the bundled `postgres` service on the Compose network pass.
- **Not pinned.** This is the one connection whose DNS answer O.D.I.N. does not pin. The check runs at start-up and each time O.D.I.N. opens the database or starts a backup tool, but the PostgreSQL driver (libpq) looks the host name up again itself whenever it connects. If the name could ever answer with an outside address, use the server's IP address or a Unix socket in `DATABASE_URL` instead of a name.

SQLite (the default) has no database connection and is not affected.

## Printer discovery (mDNS): the one multicast exception

"Find Klipper printers on the network" sends one mDNS query to the multicast group `224.0.0.251`, port 5353. That address is not a private unicast address, so it is an explicit exception to the promise above. It is a link-local multicast group: routers do not forward it, so the query stays on the network segment the server is on, and the address is fixed in the code (no setting or received packet can change it). Answers are only offered, and only contacted, after they pass the same local-network check as any printer. If your network runs an mDNS reflector or repeater, that device can relay the query to other segments you configured it for; O.D.I.N. has no control over that.

## If O.D.I.N. refuses to start because of a saved printer

With ITAR mode on, O.D.I.N. does not start while a saved printer has an IP address outside the local network; the log names the printers. The UI is not available then. Do not start O.D.I.N. with ITAR mode off to fix it: the printer monitors would start and contact those addresses before you could change them. Use the offline command instead. It only reads and writes the database: it starts no O.D.I.N. service, opens no network connection and looks no name up, and ITAR mode stays on.

1. Stop the container: `docker compose stop odin`.
2. List the printers that are refused:

       docker compose run --rm --no-deps --entrypoint python3 -w /app/backend odin -m scripts.itar_printer_addresses list

3. For each one, either save its correct local address or remove the address (the printer stays in O.D.I.N. with no address, and you can enter one in the UI later):

       docker compose run --rm --no-deps --entrypoint python3 -w /app/backend odin -m scripts.itar_printer_addresses set 4 192.168.1.60
       docker compose run --rm --no-deps --entrypoint python3 -w /app/backend odin -m scripts.itar_printer_addresses clear 5

4. Run `list` again until it reports none, then start O.D.I.N.: `docker compose up -d`.

`set` refuses an address outside the local network. A host name is accepted without being looked up; it is checked every time O.D.I.N. connects to the printer. Each change is written to the audit log (`itar.printer_address_changed`). Exit status: 0 done (for `list`: none left), 1 `list` found a printer, 2 wrong usage or a refused address, 3 the database could not be read.

With the default SQLite database you can also run the command with no network at all, which shows that it needs none. Use your image name and data folder:

    docker run --rm --network none --entrypoint python3 -w /app/backend -v "$PWD/odin-data:/data" <odin image> -m scripts.itar_printer_addresses list

In the enterprise layout the service is `api` and the command needs the database: start only it first (`docker compose -f docker-compose.enterprise.yml up -d postgres`), then use `api` in place of `odin` in the commands above.

These commands are covered by automated tests of the command itself. The `docker` lines above have not been run against a built image in testing.

## Allowing an identity provider

A cloud identity provider (Microsoft Entra ID, GCC High, Okta, Google) is public, so by default SSO cannot be used with ITAR mode on. To allow one, list its hostnames:

    ODIN_ITAR_ALLOWED_IDP_HOSTS=login.microsoftonline.us,graph.microsoft.us

Microsoft Entra ID in GCC High needs both hosts: sign-in, token and signing keys are on `login.microsoftonline.us`, and its discovery document points user info at `graph.microsoft.us`. Commercial Entra ID uses `login.microsoftonline.com` and `graph.microsoft.com`. Check your provider's discovery document (`/.well-known/openid-configuration`) and list every host its endpoints use.

The allowance is deliberately narrow:

- **Env only.** There is no UI or database setting for it. Changing it needs a restart.
- **Exact hostnames.** Comma-separated, lowercase, at most 8. Each entry must be a full DNS name with at least one dot: labels of letters, digits and hyphens (no leading or trailing hyphen, 1-63 characters), and a last label that contains a letter. No wildcards, no ports or paths, and no IP addresses in any form, including short, hexadecimal or octal forms such as `8.8` or `0x8.8.8.8`, which the resolver would read as an address. An invalid entry stops O.D.I.N. from booting.
- **OIDC sign-in only.** Only the OIDC client may use it, for four requests: discovery, token, userinfo and signing keys. Webhooks, the license server, Spoolman, SMTP, MQTT and Classroom stay refused for the same host.
- **https, port 443.** Redirects are never followed, and DNS is pinned to the validated addresses.
- **Every endpoint must be listed.** When the discovery host is listed, the discovery document's `token_endpoint`, `userinfo_endpoint` and `jwks_uri` hosts must each be listed too. A private address does not exempt an endpoint: an endpoint on an unlisted host is refused whether that host is public or private. Otherwise sign-in is refused and the reason is written to the log. Read your provider's discovery document and list every host it names. Some providers use several, and Entra's `userinfo_endpoint` may be on a different host than the login host (verify this in your tenant). A private IdP whose discovery host is not listed does not use the allowance and keeps the normal private-only rules.
- **Boot check.** If SSO is enabled and its discovery host is neither private nor listed, O.D.I.N. refuses to boot.
- **Audit.** Each OIDC request to a listed host writes an audit row `itar.idp_outbound` with the host and the endpoint kind. Tokens, codes and bodies are never recorded. If the audit row cannot be written, the request is refused.

## Microsoft GCC High

1. In the GCC High Entra portal (portal.azure.us) register an application. Set the redirect URI to `https://<odin-host>/api/auth/oidc/callback`.
2. In Settings → SSO set the discovery URL explicitly: `https://login.microsoftonline.us/<tenant-id>/v2.0/.well-known/openid-configuration`, plus the client ID and secret. A blank discovery URL falls back to the commercial endpoints.
3. With ITAR mode on, set `ODIN_ITAR_ALLOWED_IDP_HOSTS` to `login.microsoftonline.us` and to any other host in your tenant's discovery document (see above), then restart.
4. Sign in with a test user and check the log (logger `oidc`) and the `itar.idp_outbound` audit rows.

GCC High support is covered by automated tests against a stub identity provider. It has not been run against a real GCC High tenant.
