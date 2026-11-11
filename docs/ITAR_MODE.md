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
