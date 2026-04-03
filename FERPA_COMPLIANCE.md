# FERPA Compliance Statement — O.D.I.N.

**Product:** O.D.I.N. (Orchestrated Dispatch & Inventory Network)
**Vendor:** Sublab 3DP
**Version:** 1.3.27
**Date:** February 2026
**Contact:** sublab3dp@gmail.com

> **[ATTORNEY REVIEW RECOMMENDED]** This document is provided as a technical reference for institutions evaluating O.D.I.N. for FERPA compliance. It does not constitute legal advice. Institutions should consult qualified legal counsel to confirm compliance with their specific obligations under FERPA (20 U.S.C. § 1232g; 34 CFR Part 99).

---

## 1. Overview

O.D.I.N. is a self-hosted 3D printer fleet management system. It is deployed entirely on-premises within the institution's own network infrastructure. Sublab 3DP does not host, access, process, or store any institutional data.

This document describes O.D.I.N.'s architecture and data handling practices relevant to FERPA compliance for educational institutions.

---

## 2. Deployment Architecture

O.D.I.N. runs as a single Docker container on hardware controlled by the institution. There is no cloud component, SaaS tier, or hosted offering.

| Aspect | Detail |
|--------|--------|
| Deployment model | Self-hosted, on-premises Docker container |
| Cloud dependency | None |
| Telemetry / phone-home | None |
| Analytics sent to vendor | None |
| Data storage location | Local SQLite database at `/data/odin.db` |
| Network requirements | Local network only (no internet required for operation) |
| License validation | Ed25519 air-gap compatible (no internet required) |

The institution maintains full physical and logical control over all data at all times.

---

## 3. Data Collected and Stored

O.D.I.N. stores only operational data necessary for 3D printer fleet management. It does **not** collect or store education records as defined by FERPA.

### 3.1 Data O.D.I.N. Stores

| Data Element | Purpose | FERPA Education Record? |
|-------------|---------|------------------------|
| Username | Authentication and access control | No |
| Email address (school-issued) | Account identification, optional notifications | No (operational) |
| User role (admin/operator/viewer) | RBAC authorization | No |
| Print job history | Job tracking and scheduling | No |
| Approval records | Workflow audit trail | No |
| Audit log entries | Security and compliance auditing | No |
| Printer telemetry | Equipment monitoring | No |
| Filament/spool inventory | Supply management | No |
| 3D model files | Print queue management | No |

### 3.2 Data O.D.I.N. Does NOT Collect

- Student grades or academic records
- Personally identifiable information (PII) beyond username and school email
- Social Security numbers, student ID numbers, or demographic data
- Parent/guardian information
- Disciplinary records
- Financial aid or billing information
- Health records
- Biometric data

**[ATTORNEY REVIEW RECOMMENDED]** Institutions should evaluate whether print job metadata (e.g., filenames, timestamps, assignment-related model names) could constitute "education records" under their specific FERPA interpretation, particularly if print jobs are linked to graded coursework.

---

## 4. Data Controller and Processor Roles

| Role | Entity |
|------|--------|
| Data Controller | The deploying institution (school/university) |
| Data Processor | None — Sublab 3DP has no access to institutional data |

Sublab 3DP distributes O.D.I.N. as software. Once deployed, the institution is the sole controller of all data within the system. Sublab 3DP:

- Cannot access the institution's O.D.I.N. instance
- Does not receive any data from the instance
- Has no remote access, backdoor, or telemetry capability
- Does not act as a "school official" under FERPA

**[ATTORNEY REVIEW RECOMMENDED]** Because Sublab 3DP has no access to institutional data, it is unlikely that a "school official" exception or data processing agreement is required. However, institutions should confirm this with counsel, particularly if Sublab 3DP provides on-site support or consulting services that involve access to the system.

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
- **Audit logging** of all user actions

Institutions can configure roles to enforce least-privilege access. For example, students can be assigned the Viewer role to limit their access to read-only dashboards.

---

## 6. Data Portability and Deletion

O.D.I.N. provides GDPR-aligned data management endpoints that also support FERPA data subject rights:

| Endpoint | Function |
|----------|----------|
| `GET /api/v1/gdpr/export` | Export all data associated with a user account |
| `DELETE /api/v1/gdpr/erase` | Permanently delete all data associated with a user account |

These endpoints enable institutions to:

- Fulfill parent/eligible student requests to inspect records
- Delete user data when students leave the institution
- Export data for transfer to another system

**[ATTORNEY REVIEW RECOMMENDED]** Institutions should establish internal procedures for handling FERPA access and amendment requests and determine whether O.D.I.N.'s export/erasure endpoints satisfy their procedural requirements.

---

## 7. Backup and Disaster Recovery

The institution controls all backup and recovery processes:

- Database is a single SQLite file (`/data/odin.db`) using WAL mode
- Standard filesystem backup tools apply
- Backups remain within the institution's infrastructure
- No data is replicated to external servers

Institutions should include O.D.I.N. data in their existing backup and retention policies.

---

## 8. Audit Logging

O.D.I.N. maintains an audit log that tracks:

- User authentication events (login, logout, failed attempts)
- Print job submissions and approvals
- Configuration changes
- User account modifications
- API access

Audit logs are stored locally in the SQLite database and are accessible to administrators.

---

## 9. Encryption

| Layer | Method |
|-------|--------|
| Credentials at rest | Fernet symmetric encryption |
| Passwords | bcrypt hashing |
| JWT tokens | HS256 signing |
| Secrets | Auto-generated on first run, stored in `/data/` |

**[ATTORNEY REVIEW RECOMMENDED]** O.D.I.N. does not provide TLS termination natively. Institutions should deploy O.D.I.N. behind a reverse proxy (e.g., nginx, Traefik) with TLS certificates to encrypt data in transit. This is critical for FERPA compliance if the system is accessible beyond localhost.

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

O.D.I.N. does not share data with any third party. There are:

- No analytics services
- No advertising networks
- No cloud APIs
- No external webhooks enabled by default
- No telemetry or usage reporting to Sublab 3DP

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

O.D.I.N.'s self-hosted, air-gapped architecture places it firmly under institutional control. Sublab 3DP never accesses, processes, or stores institutional data. The system collects only operational data necessary for printer fleet management — not education records as defined by FERPA.

**[ATTORNEY REVIEW RECOMMENDED]** Institutions should have their legal counsel review this document and O.D.I.N.'s data handling practices to confirm compliance with their FERPA obligations before deployment.
