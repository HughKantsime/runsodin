# ODIN Education POC — CTEC Handoff

## Scope

This proof of concept gives CTEC a self-hosted ODIN Education workspace for a deliberately small pilot. Students authenticate, select an authorized class or club, and upload a sliced Bambu `.3mf`. A teacher reviews the submission and assigns an entitled compatible printer. Tenant admins manage cost centers, rosters, printer entitlements, Google or Entra OIDC, and optional Google Classroom import.

This is not a claim that every supplied printer is certified. The initial physical path is one or more approved Bambu X1 Carbon or P1S printers using sliced `.3mf`. Creality K1 requires a sliced Moonraker-compatible `.gcode` expansion path and live certification. H2S, H2C, and the reported X-one² require model/protocol confirmation and physical evidence before use.

## CTEC context supplied for planning

- Approximately 354 students and 20 staff.
- Printer inventory supplied: six X1 Carbon, seven Creality K1, seventeen P1S, three H2S, two H2C, plus an X-one² entry whose exact model/count must be confirmed.
- Identity options: Google Workspace, Entra, and AD. Google OIDC is the preferred POC path; Entra remains supported.
- Deployment: ODIN container on a VM, with separate server, user, and printer VLANs.
- Students slice locally in OrcaSlicer; cloud slicing and Google Classroom coursework are outside scope.

## VM and container prerequisites

1. Use a dedicated supported Linux VM with Docker Engine/Compose, persistent ODIN data storage, working DNS/NTP, and enough free space for two verified database copies plus uploaded pilot files.
2. Give ODIN a stable HTTPS hostname trusted by the school. Do not expose the private production hostname in public evidence.
3. Set `TRUSTED_HOSTS` to the exact hostname, `COOKIE_SECURE=true`, and `COOKIE_SAMESITE=lax` for OIDC redirects. Set `OIDC_REDIRECT_URI` to `https://HOST/api/auth/oidc/callback`.
4. Generate and securely retain `JWT_SECRET_KEY` and `ENCRYPTION_KEY`. Transfer them through CTEC's approved secret channel, never email or the handoff report.
5. Install the signed Education entitlement, enable Education mode, then run database migrations by starting the exact candidate image normally.

## Firewall matrix

| Source | Destination | Port | Purpose |
|---|---|---:|---|
| User VLAN | ODIN HTTPS hostname | TCP 443 | Browser UI/API |
| ODIN VM | Google identity/Classroom APIs | TCP 443 | Optional Google OIDC and Classroom |
| ODIN VM | Entra endpoints | TCP 443 | Optional Entra OIDC |
| ODIN VM | DNS/NTP | Site policy | Name resolution and correct token time |
| ODIN VM | Approved Bambu pilot printers | TCP 8883 | TLS MQTT status/control |
| ODIN VM | Approved Bambu pilot printers | TCP 990 | FTPS upload |
| All other cross-VLAN flows | — | — | Deny unless separately documented and approved |

Use explicit VM-to-printer rules for only the pilot device addresses. Do not allow printers to initiate general access to the server or user VLAN.

## Google login

1. Create a Google web OAuth client.
2. Add `https://HOST/api/auth/oidc/callback` as an authorized redirect URI.
3. In ODIN Settings → Access → Authentication, select Google Workspace, enter the client ID/secret, set the CTEC Workspace domain, choose the ODIN tenant, keep login scopes at `openid profile email`, and enable SSO.
4. Test with one synthetic or explicitly approved staff account and one student pilot account. ODIN validates Google signature, audience, issuer, expiry, one-time nonce, verified email, and signed hosted-domain claim.

Entra can be selected in the same screen instead. Google login and Classroom authorization are intentionally separate.

## Optional Google Classroom import

1. In Google Cloud, enable the Google Classroom API for the OAuth project and add `https://HOST/api/education/classroom/callback` as another authorized redirect URI.
2. Open Education → Google Classroom. Enter the web OAuth client and CTEC Workspace domain, save, then authorize with a tenant-admin Google account that can see the pilot course.
3. ODIN requests only course-read, roster-read, profile-email, and identity scopes. It does not request coursework, grades, roster writes, domain-wide delegation, or background access beyond the retained refresh token.
4. Select a course, review the complete teacher/student preview and diff, then explicitly import. Teachers receive cost-center manager grants and students receive student grants; all new global ODIN roles remain viewer.
5. A later confirmed sync revokes removed memberships for that mapped class. It does not delete users, submissions, other class memberships, or printer entitlements.

## Pilot setup

1. Start with one class or club, one teacher, a small approved student group, and one or two Bambu X1/P1 printers.
2. Add only non-shared, active printers to the cost center.
3. Students use OrcaSlicer to produce sliced Bambu `.3mf` files. Raw models and K1 `.gcode` are not interchangeable with this POC path.
4. Teacher acceptance: view only the managed class queue, inspect compatibility, approve to an entitled printer or reject with a reason, and observe the job lifecycle.

## Backup, restore, and teardown

- Create and verify a backup before roster import and again after the pilot configuration is accepted. Follow `docs/BACKUP_RESTORE_RUNBOOK.html` and retain the encryption/JWT keys separately from the database backup.
- A Classroom disconnect removes retained Google access/refresh tokens but deliberately preserves imported users, cost centers, grants, and submissions.
- User erasure removes direct Classroom identity data, revokes active education grants, clears a matching connected Google identity/token set, and retains only required operational/audit facts in tombstoned form.
- For a disposable sandbox, use the existing exact-ID `edu-sandbox-reset`/`edu-sandbox-purge` commands. Do not run them against production data.

## Preflight

From the candidate checkout, with the deployment environment loaded:

```sh
make ctec-poc-preflight REQUIRE_CLASSROOM=1
```

Review `artifacts/ctec-poc-preflight/ctec-poc-preflight.html`. The command is read-only: it performs no OAuth consent, import, printer action, deployment, or reset, and its output contains configuration states rather than secret values.

## Acceptance script

1. Restore or start a disposable exact-image instance and confirm HTTPS/session posture.
2. Sign in as tenant admin through the selected OIDC provider.
3. Import or manually create the pilot cost center and verify teacher/student role projections.
4. Entitle only the approved pilot Bambu printer(s).
5. Sign in as a student; upload a sliced `.3mf`; verify only authorized centers and the student's own submission appear.
6. Sign in as the teacher; preview compatibility; reject one test submission and approve one to an entitled printer.
7. Observe the approved lifecycle without claiming physical success until the printer actually reports it.
8. Create and verify a backup, restore it into a disposable instance, and confirm mapping/grants/submissions survive while tokens remain encrypted.
9. Run preflight and retain its HTML/JSON plus the exact image digest and test evidence.

## Known limitations at handoff

- Live Google consent still requires CTEC's OAuth client, hostname, and approved test account; repository tests use protocol-realistic mocked Google responses and do not pretend to be live consent evidence.
- Google Classroom rostering is read-only and explicitly initiated. No automatic sync, assignments, grades, or course mutations.
- Bambu sliced `.3mf` is the POC submission/dispatch boundary. K1/Moonraker `.gcode`, H2S/H2C, and X-one² are expansion/certification work.
- Production activation, CTEC network access, real student data, and live printer commands remain separate ship-gate actions.
