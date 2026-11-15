# FERPA Compliance Statement — O.D.I.N.

**Product:** O.D.I.N. (Orchestrated Dispatch & Inventory Network)
**Vendor:** JLLS Holdings LLC, d/b/a SubLab 3DP
**Version:** 1.3.27
**Date:** February 2026
**Revised:** November 2026. Network connection and third-party sections reviewed against O.D.I.N. 2.2.0; other sections as issued for 1.3.27.
**Contact:** support@runsodin.com

> This document is provided as a technical reference for institutions evaluating O.D.I.N. for FERPA compliance. It does not constitute legal advice. Institutions should consult qualified legal counsel to confirm compliance with their specific obligations under FERPA (20 U.S.C. § 1232g; 34 CFR Part 99).

---

## 1. Overview

O.D.I.N. is a self-hosted 3D printer fleet management system. It is deployed entirely on-premises within the institution's own network infrastructure. Operational data is stored on the institution's infrastructure. License actions send the data described in Section 2 to Sublab 3DP; they do not send student records.

This document describes O.D.I.N.'s architecture and data handling practices relevant to FERPA compliance for educational institutions.

---

## 2. Deployment Architecture

O.D.I.N. runs as a single Docker container on hardware controlled by the institution. Local operation does not require a cloud service; administrator-run license actions use the license portal as described below.

| Aspect | Detail |
|--------|--------|
| Deployment model | Self-hosted, on-premises Docker container |
| Cloud dependency | None |
| Telemetry / phone-home | No vendor telemetry or background vendor calls. License actions an administrator runs contact runsodin.com. Online activation sends the license key, installation ID and public device key. Starting a trial sends the installation ID, public device key and administrator-entered email address. Re-checking sends the license key, installation ID and current expiry when present, plus the public device key for device-bound paid licenses; moving a key sends the license key and installation ID. Requests also carry authentication proofs and protocol fields. Activation and move request files additionally contain the server's hostname, O.D.I.N. version and generation timestamp. The private device key is never transmitted. The portal sees the connecting IP address. These actions do not send print, job, user or student records. Activation, starting a trial and moving a key can be done without a connection from O.D.I.N., using another computer to contact the portal; re-checking requires a connection from O.D.I.N. |
| Analytics sent to vendor | None |
| Data storage location | Local SQLite database at `/data/odin.db` |
| Network requirements | Local network only by default; integrations and license actions you run use the internet |
| License validation | An installed license is verified locally (Ed25519), no internet required; activation can be done by file |

The institution controls data stored in its deployment and which integrations it enables; outbound connections are described in Section 11.

---

## 3. Data Collected and Stored

O.D.I.N. stores operational data and configured school records; the institution must determine which records are education records in its deployment.

### 3.1 Data O.D.I.N. Stores

| Data Element | Purpose |
|-------------|---------|
| Username | Authentication and access control |
| Email address (school-issued) | Account identification, optional notifications |
| User role (admin/operator/viewer) | RBAC authorization |
| Print job history | Job tracking and scheduling |
| Approval records | Workflow audit trail |
| Audit log entries | Security and compliance auditing |
| Printer telemetry | Equipment monitoring |
| Filament/spool inventory | Supply management |
| 3D model files | Print queue management |

When roster import is enabled, O.D.I.N. receives provider user identifiers, email addresses and class memberships; Google Classroom import also receives names.

### 3.2 Data O.D.I.N. Does NOT Collect

- Student grades or academic records
- Social Security numbers or demographic data
- Parent/guardian information
- Disciplinary records
- Financial aid or billing information
- Health records
- Biometric data

Institutions should evaluate whether print job metadata (e.g., filenames, timestamps, assignment-related model names) could constitute "education records" under their specific FERPA interpretation, particularly if print jobs are linked to graded coursework.

---

## 4. Data Controller and Processor Roles

| Role | Entity |
|------|--------|
| Data Controller | The deploying institution (school/university) |
| Vendor access | Sublab 3DP receives the license-action data described in Section 2; access to the instance requires the institution to grant it |

Sublab 3DP distributes O.D.I.N. as software. Once deployed, the institution is the sole controller of all data within the system. Sublab 3DP:

- Has no access to the institution's O.D.I.N. instance unless the institution grants it
- Receives the license-action data described in Section 2, but no student records through those actions
- Has no remote access, backdoor, or telemetry capability
- Does not act as a "school official" under FERPA

Institutions should determine with counsel whether a "school official" exception or data processing agreement is required, considering the license-action data and any support or consulting services that involve access to the system.

---

## 5. Access Controls (RBAC)

O.D.I.N. implements role-based access control with three tiers:

| Role | Permissions |
|------|------------|
| **Admin** | Full system access: user management, configuration, all data |
| **Operator** | Printer operations, job management, model uploads |
| **Viewer** | Read-only access to dashboards and job status |

Additional access control features:

- **JWT authentication** with 24-hour token expiry and bcrypt password hashing
- **API key** perimeter authentication
- **OIDC SSO** integration for institutional identity providers
- **Session management** with active session tracking and token blacklisting
- **Audit logging** — Selected authentication, operational and administrative actions are logged

Institutions can configure roles to enforce least-privilege access. For example, students can be assigned the Viewer role to limit their access to read-only dashboards.

---

## 6. Data Portability and Deletion

O.D.I.N. provides GDPR-aligned data management endpoints that also support FERPA data subject rights:

| Endpoint | Function |
|----------|----------|
| `GET /api/v1/users/{user_id}/export` | Export supported user data |
| `DELETE /api/v1/users/{user_id}/erase` | Anonymize supported user data; retained operational records and excluded copies are not all deleted |

These endpoints enable institutions to:

- Fulfill parent/eligible student requests to inspect records
- Anonymize supported user data when students leave the institution
- Export data for transfer to another system

Institutions should establish internal procedures for handling FERPA access and amendment requests and determine whether O.D.I.N.'s export/erasure endpoints satisfy their procedural requirements.

---

## 7. Backup and Disaster Recovery

The institution controls all backup and recovery processes:

- Database is a single SQLite file (`/data/odin.db`) using WAL mode
- Standard filesystem backup tools apply
- Backups remain within the institution's infrastructure
- O.D.I.N. does not replicate its database or backups to any external server

Institutions should include O.D.I.N. data in their existing backup and retention policies.

---

## 8. Audit Logging

Selected authentication, operational and administrative actions are logged, including:

- User authentication events (login, logout, failed attempts)
- Print job submissions and approvals
- Configuration changes
- User account modifications
- Designated API actions; not every request

Audit logs are stored locally in the installation's database and are accessible to authorized administrators. If SIEM streaming is enabled, copies are sent to the configured destination.

---

## 9. Encryption

| Layer | Method |
|-------|--------|
| Credentials at rest | Fernet symmetric encryption |
| Passwords | bcrypt hashing |
| JWT tokens | HS256 signing |
| Secrets | Auto-generated on first run, stored in `/data/` |

O.D.I.N. does not provide TLS termination natively. Institutions should deploy O.D.I.N. behind a reverse proxy (e.g., nginx, Traefik) with TLS certificates to encrypt data in transit. This is critical for FERPA compliance if the system is accessible beyond localhost.

---

## 10. Recommended Deployment Practices for FERPA Compliance

1. **Deploy on-premises** within the institution's network, behind a firewall
2. **Use TLS** via a reverse proxy for all HTTP traffic
3. **Integrate with institutional SSO** via OIDC to leverage existing identity management
4. **Assign least-privilege roles** — students as Viewers, staff as Operators, IT as Admins
5. **Establish data retention policies** — use the GDPR erasure endpoint when students depart
6. **Include O.D.I.N. data in existing backup procedures**
7. **Restrict network access** — limit O.D.I.N. to the campus network or VPN
8. **Review audit logs** periodically for unauthorized access
9. **Use anonymized usernames** for students where possible (e.g., "student_01")

---

## 11. Third-Party Data Sharing

License actions send the data described in Section 2 to the vendor, but no student records through those actions. Enabled integrations and push notifications can send data to their configured services. Opening the API documentation causes your browser to request assets from jsDelivr and FastAPI's website; ReDoc also requests Google Fonts. There are:

- No analytics services
- No advertising networks
- No cloud APIs in a default installation
- No external webhooks enabled by default
- No telemetry or usage reporting to Sublab 3DP

**Integrations the institution may turn on, and what leaves the server when it does:** class roster import from Google Classroom or an LTI platform such as Canvas (roster import uses read-only scopes and does not write roster changes back; O.D.I.N. sends credentials, class identifiers and authorization parameters; LTI sign-in also returns the platform-provided login hint and optional message hint through the browser); single sign-on (sign-in is handled by the institution's identity provider); push notifications, if a user turns them on (the notification text, which can include a job or file name, passes through the browser vendor's push service, or through Apple's push service where the server operator has set that up); notification services and email the institution configures; Square and Spoolman (product stock and filament data, no student records).

**Note:** O.D.I.N. supports optional webhook integrations (Discord, Slack, email, ntfy, Telegram) for print notifications. If enabled by the institution, notification data is sent to the configured external service. Institutions should evaluate whether notification content could include student information.

---

## 12. Incident Response

Because O.D.I.N. is self-hosted, the institution is responsible for incident response, including:

- Monitoring for unauthorized access via audit logs
- Responding to data breaches per institutional policy and FERPA requirements
- Notifying affected parties as required by applicable law

Sublab 3DP provides security patches and updates via new Docker image releases. Institutions should apply updates promptly.

---

## Summary

O.D.I.N.'s self-hosted, air-gapped architecture places it firmly under institutional control. Sublab 3DP receives the license-action data described in Section 2; it has no access to the instance unless the institution grants it. O.D.I.N. stores operational data and configured school records; the institution must determine which records are education records in its deployment.

Institutions should have their legal counsel review this document and O.D.I.N.'s data handling practices to confirm compliance with their FERPA obligations before deployment.
