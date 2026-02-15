# COPPA Compliance Statement — O.D.I.N.

**Product:** O.D.I.N. (Orchestrated Dispatch & Inventory Network)
**Vendor:** Sublab 3DP
**Version:** 1.3.27
**Date:** February 2026
**Contact:** sublab3dp@gmail.com

> **[ATTORNEY REVIEW RECOMMENDED]** This document is provided as a technical reference for K-12 schools evaluating O.D.I.N. for COPPA compliance. It does not constitute legal advice. Schools should consult qualified legal counsel to confirm compliance with their specific obligations under COPPA (15 U.S.C. §§ 6501-6506; 16 CFR Part 312).

---

## 1. Overview

COPPA regulates the online collection of personal information from children under 13. O.D.I.N. is a self-hosted 3D printer fleet management system deployed and operated entirely by the school on its own infrastructure. Sublab 3DP does not operate a website or online service directed at children and does not collect personal information from children.

This document explains how O.D.I.N.'s architecture and data handling practices support COPPA compliance for K-12 deployments.

---

## 2. Operator Roles Under COPPA

| COPPA Role | Entity | Rationale |
|-----------|--------|-----------|
| **Operator** | The deploying school | The school installs, configures, and operates O.D.I.N. on its own hardware. The school controls all user accounts, data collection, and access. |
| **Not an Operator** | Sublab 3DP | Sublab 3DP distributes software. It does not operate a service, does not collect data from children, and has no access to any deployed instance. |

Under COPPA, the school acts as the operator and may provide consent on behalf of parents/guardians for the collection of student information in the school context, consistent with the FTC's guidance on school authorization (16 CFR § 312.5(c)(1)).

**[ATTORNEY REVIEW RECOMMENDED]** Schools should confirm that their use of O.D.I.N. falls within the "school authorization" exception and that their consent practices comply with COPPA requirements. The FTC has stated that schools may consent on behalf of parents when the information is used solely for school-authorized educational purposes.

---

## 3. Data Collection Practices

### 3.1 What O.D.I.N. Collects

O.D.I.N. collects only the data necessary to manage 3D printer operations:

| Data Element | Required? | Can Be Anonymized? | Notes |
|-------------|-----------|-------------------|-------|
| Username | Yes | Yes | Can use non-identifying labels (e.g., "student_01", "maker_lab_7") |
| Email address | No | Yes / Omittable | Can be disabled entirely for student accounts |
| User role | Yes | N/A | System role (viewer/operator/admin), not personal information |
| Print job records | Yes | Partially | Job metadata linked to username; anonymized usernames reduce identifiability |
| Approval records | If enabled | Partially | Tracks who approved print jobs |

### 3.2 What O.D.I.N. Does NOT Collect

- Full legal names (unless used as usernames — avoidable)
- Home addresses, phone numbers, or parent/guardian contact information
- Social Security numbers or government identifiers
- Photographs, video, or audio of students
- Geolocation data
- Persistent identifiers for tracking across sites or services
- Cookies sent to third parties
- Any data transmitted to Sublab 3DP or any third party

### 3.3 No Third-Party Data Sharing

O.D.I.N. does not transmit any data to:

- Sublab 3DP
- Advertising networks
- Analytics services
- Data brokers
- Any external server (unless the school explicitly configures webhook integrations)

---

## 4. No Direct Collection from Children by Sublab 3DP

Sublab 3DP does not interact with children at any point:

- O.D.I.N. is installed by school IT staff, not by students
- User accounts are created by school administrators, not by students self-registering
- Sublab 3DP has no access to deployed instances
- There is no cloud service, registration portal, or website where children provide information to Sublab 3DP
- License validation uses air-gap Ed25519 signatures — no internet connection or data exchange with Sublab 3DP is required

---

## 5. Parental Consent

Because the school is the operator under COPPA:

- The **school provides consent** on behalf of parents for the use of student information within O.D.I.N., per COPPA's school authorization provision
- The school should include O.D.I.N. in its technology consent disclosures to parents
- Parents retain the right to review their child's information and request deletion — the school can fulfill these requests using O.D.I.N.'s data export and erasure endpoints

**[ATTORNEY REVIEW RECOMMENDED]** Schools should verify that their existing parental consent processes cover the use of O.D.I.N. and that parents are informed about the system's data practices. The FTC requires that the school's consent be based on the operator's data collection practices.

---

## 6. Recommended Practices for Under-13 Deployments

Schools deploying O.D.I.N. for students under 13 should adopt the following practices:

### 6.1 User Account Configuration

- **Use anonymized usernames** — assign non-identifying labels such as "student_01", "maker_A3", or class-period codes instead of real names
- **Disable or omit email fields** — email is not required for O.D.I.N. to function; leave it blank for under-13 accounts
- **Assign the Viewer role** — restrict students to read-only access; have teachers/staff submit print jobs on their behalf if needed

### 6.2 Access Controls

- **Use OIDC SSO** — integrate with the school's existing identity provider rather than managing separate credentials
- **Restrict network access** — limit O.D.I.N. to the school's internal network; do not expose it to the public internet
- **Review the audit log** — periodically check for unauthorized access or account misuse

### 6.3 Data Retention and Deletion

- **Establish a retention schedule** — delete student accounts and associated data at the end of each school year or when students leave
- **Use the GDPR erasure endpoint** (`DELETE /api/v1/gdpr/erase`) to permanently remove all data associated with a student's account
- **Use the GDPR export endpoint** (`GET /api/v1/gdpr/export`) to fulfill parental inspection requests

### 6.4 Notification Webhooks

- **Disable or restrict webhook integrations** for under-13 deployments, as notifications sent to external services (Discord, Slack, etc.) could include student-associated data
- If webhooks are used, ensure notification content does not include student-identifiable information

---

## 7. Data Security

O.D.I.N. protects stored data with:

| Measure | Detail |
|---------|--------|
| Password hashing | bcrypt |
| Credential encryption | Fernet symmetric encryption at rest |
| Authentication | JWT tokens (HS256, 24-hour expiry) + API key |
| Role-based access | Admin / Operator / Viewer tiers |
| Session management | Active session tracking, token blacklisting |
| Audit logging | All user actions logged |

**[ATTORNEY REVIEW RECOMMENDED]** Schools should deploy O.D.I.N. behind a TLS-terminating reverse proxy (nginx, Traefik, etc.) to encrypt data in transit. O.D.I.N. does not provide native TLS. This is important for protecting children's information in transit on the network.

---

## 8. Data Breach Response

The school is responsible for data breach notification as O.D.I.N. is self-hosted. In the event of a breach involving children's personal information:

- The school should follow its existing breach notification procedures
- COPPA requires operators to maintain reasonable security; the school's IT practices should include monitoring and incident response for O.D.I.N.
- Sublab 3DP provides security patches via Docker image updates; schools should apply them promptly

---

## 9. No Advertising or Behavioral Tracking

O.D.I.N. contains:

- No advertising of any kind
- No behavioral tracking or profiling
- No cookies used for third-party tracking
- No fingerprinting or persistent identifiers sent externally
- No gamification designed to encourage excessive data sharing

---

## 10. Vendor Attestation

Sublab 3DP attests that:

1. O.D.I.N. does not collect personal information directly from children
2. O.D.I.N. does not transmit any data to Sublab 3DP or any third party
3. O.D.I.N. does not contain advertising or third-party tracking
4. O.D.I.N.'s license validation is air-gap compatible and does not require data exchange
5. Sublab 3DP has no access to any deployed O.D.I.N. instance

**[ATTORNEY REVIEW RECOMMENDED]** Schools should maintain a copy of this attestation and any supplementary agreements with Sublab 3DP as part of their COPPA compliance documentation.

---

## Summary

O.D.I.N. is self-hosted software that operates entirely under the school's control. Sublab 3DP does not collect, access, or process children's personal information. Schools can configure O.D.I.N. to minimize data collection through anonymized usernames, disabled email fields, and restricted roles — supporting COPPA compliance by design.

**[ATTORNEY REVIEW RECOMMENDED]** Schools should have their legal counsel review this document and confirm that their deployment configuration and consent practices satisfy COPPA requirements before deploying O.D.I.N. for students under 13.
