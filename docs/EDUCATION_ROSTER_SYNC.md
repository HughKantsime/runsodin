# Roster sync and classroom sign-in (D4)

Education and Enterprise include `roster_sync`. This feature ships disabled and has
no embedded credentials. It also requires the existing `education_workflows` and
`classes` entitlements. Settings → Integrations → Roster sync is available to an
interactive **school administrator**. Platform administrators must use an account
in the school; neither teachers nor API tokens can configure/import. The permission
matrix's `education.roster_manage` key can narrow access but cannot bypass that floor.

## Setup: NEEDS SHANE / school administrator

Use the school's or SubLab's existing Google Cloud OAuth client / Canvas developer
registration. No paid API, new account, or provider fallback is required by ODIN.
Enter credentials in Settings, never in source control. A functioning installation
Fernet encryption key and public HTTPS origin are required. Saving defaults to
disabled; explicitly check Enable when the school's configuration is ready.
Saving configuration invalidates outstanding receipts. Provider authorization is retained
unless credentials or provider endpoints change.
Leaving the secret blank retains the encrypted existing value. After the first import,
client ID and Canvas issuer/deployment cannot change: existing subjects must never
be reassigned to a different identity namespace. Credential/key rotation within the
same namespace remains supported.

Google: enter the client ID, secret, allowed Workspace email domains, and ODIN HTTPS
origin. Save, register the displayed callback URL in the Google OAuth client, enable,
and select Connect Google Classroom. The grant requests read-only courses, rosters
and profile emails, plus OpenID email. Authorization uses a single-use state,
browser binding and PKCE. The displayed student sign-in URL uses the same client
with only OpenID/email. Only identities imported by an admin can sign in.

Canvas: enter client ID, issuer, deployment ID, authorization/token/JWKS endpoints,
Canvas API origin, allowed email domains, and the tool's RSA private key and key ID.
The private key belongs to the ODIN LTI tool; **do not enter Canvas's signing key**.
Register the displayed login-initiation, redirect/target-link and tool JWKS URLs
with the developer key. Enable the NRPS context-membership read scope. Use a new-window
launch: third-party iframe cookie restrictions can prevent browser binding.
The first signed course launch registers its context for admin preview but does not
provision an account or grant access. The admin imports it before student sign-in.

Canvas launches require an RS256 signature from the configured JWKS, matching
issuer, audience (and authorized party for multiple audiences), deployment,
message type/version, nonce, expiry and exact target URI. Clock skew has a 60-second
leeway; launches older than ten minutes are refused. Login initiation supports GET
and POST; launch forms may contain additional LMS fields. NRPS uses a short-lived
RS256 client assertion and accepts pagination only on the configured API origin.
Neither redirects nor token-supplied key URLs are followed. Provider destinations
must resolve only to public addresses, which are DNS-pinned; environment proxies are
not used. Private-network Canvas deployments are refused. Existing ODIN ITAR egress
policy also applies. Requests use the shared worker pool with resolution and
pinning in the same thread as the synchronous httpx client. Regression tests exercise
real httpx socket resolution with connect intercepted; no real Google/Canvas requests
are made by the tests. All provider calls use `core.outbound_http.pinned_async_request` with its bounded
worker pool, a single 15-second total deadline (including slot wait, DNS, connection
and response streaming), and `max_response_bytes=2 * 1024 * 1024`. The
`public_only` guard retains the roster public-address and no-proxy policy.

## Import workflow and history

1. Load provider courses (Canvas courses appear after their first signed launch).
2. Select a course and run Dry run roster import. Review new accounts, added roles
   and revoked roles, skipped entries, and each proposed existing-account link.
   Nothing in the D2 class/user/grant graph changes during preview. Each existing
   account link shows the ODIN username, provider email and class role. Confirm or
   decline each link; import is enabled when every link has a decision. Declined
   links are skipped and reported, without creating an identity or grant.
3. Confirm within ten minutes. Confirmation imports **exactly the reviewed snapshot**,
   not a newly fetched roster. Refresh the dry run to pick up later upstream changes.
   The receipt is single-use and binds the school, admin, configuration revision and
   local roster state. Changes require another preview. Import is one transaction.

Each external course owns a dedicated D2 class and cost center. Repeated imports
update the same class title/term and grants. Unchanged active grants retain their
original timestamps and actors. Only leaving grants are revoked; missing grants
are inserted or reactivated. Imports never delete historical rows.
Teachers and Canvas teaching assistants become class managers; students receive student grants; imported ODIN
accounts have the viewer platform role. No printers are assigned automatically:
assign printers to the new cost center using existing Education administration.
Manual grants on an imported center are included in the preview and replaced on
confirmation. A center shared with a second D2 class is refused to avoid changing
another class. Archived classes/centers cannot be silently reactivated by import.
Existing legacy Google Classroom cost-center imports remain separate; D4 does not
silently adopt or overwrite those mappings.

Provider subjects are the sign-in identity. Google may link a same-school active,
passwordless, MFA-free, unbound viewer placeholder with a unique email only when
preview explicitly lists “Link existing account #id” and that account is confirmed.
Canvas never adopts a sign-in identity by email. Canvas matches and credentialed
or privileged Google matches offer a separately confirmed **grant-only** link:
cost-center membership without a new sign-in identity. These accounts keep their
existing authentication. A Canvas email match to a passwordless ODIN placeholder
also becomes grant-only: it cannot sign in through Canvas, and needs an existing
ODIN authentication method. An account already linked on another connection can
receive a confirmed grant-only link; a second subject for that account on the same
connection is refused. Inactive, ambiguous or foreign-tenant matches are refused.
Members without usable email, outside allowed domains, or without a recognized
Canvas role are skipped and reported. Canvas teaching assistants include the full
`.../lis/v2/membership/Instructor#TeachingAssistant` URI and short forms. Subsequent
sign-in requires an active imported viewer and an active grant in a mapped class.
Existing MFA policy and session issuance run through ODIN's normal OIDC exchange.
Disabling the connection blocks pending exchanges and future sign-ins/imports;
already-issued ODIN sessions follow the existing session revocation controls.

## Storage and migration

`051_roster_sync` adds connections, course mappings, external identities, grant-only
links, erasure tombstones and short-lived encrypted receipts. It creates no PostgreSQL functions or triggers.
The post027 registry validates it on every boot. PostgreSQL applies it after the
037 retained-membership step and D2's 049, matching D2's ordering and temporary
shape-check pattern. Merge-time migration renumbering must update the registry,
migrator allowlist, migration ID and tests. Released ledgers are not rewritten.
The pre-037 test harness derives dependent migrations from the live registry and
disables training 048, classes 049, program reports 050 and roster 051.
Merge D4 erase_roster calls into D5’s gdpr_erasure.py. Retiring the legacy
routes_classroom integration remains a later product decision.

Google authorization records the ODIN administrator who connected it. Erasing that
person removes the refresh token while retaining the connection; another school
administrator must reconnect Google. Merely creating the configuration does not
make someone the owner of another administrator’s Google authorization.

Erasure suppression uses a connection-scoped HMAC-SHA-256 keyed by the installation
encryption secret. Preserve that secret with backups. Settings → Roster sync →
Dry run roster import shows erased-account skips with a Clear suppression action
on each entry. An opaque handle binds that action to this preview and administrator;
it expires with the preview after ten minutes and cannot be reused. Clearing is
audited with the actor, connection and hash, and requires a fresh dry run before
import. The preview contains no erased email, subject or suppression hash; the
hash-to-handle mapping stays inside the encrypted preview receipt. Anonymous sign-in starts do not create audit rows; the unused
in-process counter has been removed.

See [the privacy map](EDU_PRIVACY_DATA_MAP.md) for export/erasure and retained history.
No scheduled/background sync, LMS grade passback, cross-provider identity merging,
or migration of legacy Google imports is included in D4.

Release handoff: Claude must run PostgreSQL fresh/pre037/upgrade cases and review
the diff. The external `odin-site` pricing page is outside this checkout; proposed
Education copy: “Classes, assignments, and reviewed Google Classroom / Canvas roster
imports with classroom sign-in.” Enterprise includes those Education capabilities.
