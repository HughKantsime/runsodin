# ITAR mode

`ODIN_ITAR_MODE=1` is an outbound network policy. It supports air-gapped and CMMC-style deployments. It is not a certification, and no part of O.D.I.N. is a CMMC, FedRAMP or ITAR certification. See [CONTROLLED_PARTS.md](CONTROLLED_PARTS.md) for the separate controlled-part labels.

It is set in the container environment, not in the UI or the database, so a compromised admin account cannot turn it off.

## What it blocks

- At boot, O.D.I.N. refuses to start if a configured outbound destination (license server, Spoolman, webhooks, SMTP, MQTT republish, an enabled SSO discovery URL) resolves to a public address.
- At run time, every outbound request is checked again and its DNS answer is pinned for the connection. A public answer, or a mix of private and public answers, is refused.
- Environment proxy settings are ignored, so identity-provider traffic needs a direct route.
- Push (APNs) and the hardcoded third-party notification APIs stay off.

Private (RFC 1918, loopback, link-local) destinations are unaffected.

## Allowing an identity provider

A cloud identity provider (Microsoft Entra ID, GCC High, Okta, Google) is public, so by default SSO cannot be used with ITAR mode on. To allow one, list its hostnames:

    ODIN_ITAR_ALLOWED_IDP_HOSTS=login.microsoftonline.us

The allowance is deliberately narrow:

- **Env only.** There is no UI or database setting for it. Changing it needs a restart.
- **Exact hostnames.** Comma-separated, lowercase, at most 8. No wildcards, no IP addresses, no ports or paths. An invalid entry stops O.D.I.N. from booting.
- **OIDC sign-in only.** Only the OIDC client may use it, for four requests: discovery, token, userinfo and signing keys. Webhooks, the license server, Spoolman, SMTP, MQTT and Classroom stay refused for the same host.
- **https, port 443.** Redirects are never followed, and DNS is pinned to the validated addresses.
- **Every endpoint must be listed.** The discovery document's `token_endpoint`, `userinfo_endpoint` and `jwks_uri` hosts must each be listed (or private). Otherwise sign-in is refused and the reason is written to the log. Read your provider's discovery document and list every host it names. Some providers use several, and Entra's `userinfo_endpoint` may be on a different host than the login host (verify this in your tenant).
- **Boot check.** If SSO is enabled and its discovery host is neither private nor listed, O.D.I.N. refuses to boot.
- **Audit.** Each OIDC request to a listed host writes an audit row `itar.idp_outbound` with the host and the endpoint kind. Tokens, codes and bodies are never recorded. If the audit row cannot be written, the request is refused.

## Microsoft GCC High

1. In the GCC High Entra portal (portal.azure.us) register an application. Set the redirect URI to `https://<odin-host>/api/auth/oidc/callback`.
2. In Settings → SSO set the discovery URL explicitly: `https://login.microsoftonline.us/<tenant-id>/v2.0/.well-known/openid-configuration`, plus the client ID and secret. A blank discovery URL falls back to the commercial endpoints.
3. With ITAR mode on, set `ODIN_ITAR_ALLOWED_IDP_HOSTS` to `login.microsoftonline.us` and to any other host in your tenant's discovery document (see above), then restart.
4. Sign in with a test user and check the log (logger `oidc`) and the `itar.idp_outbound` audit rows.

GCC High support is covered by automated tests against a stub identity provider. It has not been run against a real GCC High tenant.
