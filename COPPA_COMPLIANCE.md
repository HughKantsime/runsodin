# COPPA Compliance Statement — O.D.I.N.

**Product:** O.D.I.N. (Orchestrated Dispatch & Inventory Network)
**Vendor:** JLLS Holdings LLC, d/b/a SubLab 3DP
**Version:** 1.3.27
**Date:** February 2026
**Revised:** November 2026. Network connection and third-party sections reviewed against O.D.I.N. 2.2.0; other sections as issued for 1.3.27.
**Contact:** support@runsodin.com

> This document is provided as a technical reference for K-12 schools evaluating O.D.I.N. for COPPA compliance. It does not constitute legal advice. Schools should consult qualified legal counsel to confirm compliance with their specific obligations under COPPA (15 U.S.C. §§ 6501-6506; 16 CFR Part 312).

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

Schools should confirm that their use of O.D.I.N. falls within the "school authorization" exception and that their consent practices comply with COPPA requirements. The FTC has stated that schools may consent on behalf of parents when the information is used solely for school-authorized educational purposes.

---

## 3. Data Collection Practices

### 3.1 What O.D.I.N. Collects

O.D.I.N. stores operational data and configured school records:

| Data Element | Required? | Can Be Anonymized? | Notes |
|-------------|-----------|-------------------|-------|
| Username | Yes | Yes | Can use non-identifying labels (e.g., "student_01", "maker_lab_7") |
| Email address | No | Yes / Omittable | Can be left out for accounts an administrator creates by hand; class roster import and single sign-on provisioning bring in the email address |
| User role | Yes | N/A | System role (viewer/operator/admin), not personal information |
| Print job records | Yes | Partially | Job metadata linked to username; anonymized usernames reduce identifiability |
| Approval records | If enabled | Partially | Tracks who approved print jobs |

When roster import is enabled, O.D.I.N. receives provider user identifiers, email addresses and class memberships; Google Classroom import also receives names.

When Education features are used, O.D.I.N. can store assignment grades, feedback, assessment history, and training quiz scores and completion records.

### 3.2 What O.D.I.N. Does NOT Collect

- Home addresses, phone numbers, or parent/guardian contact information for student accounts (the optional order and shipping features hold customer addresses; those are business records, not student account fields)
- Social Security numbers or government identifiers
- Photographs, video, or audio of students
- Geolocation data
- Persistent identifiers for tracking across sites or services
- Cookies sent to third parties
- Any student data transmitted to Sublab 3DP

### 3.3 Network Connections and Third-Party Data Sharing

License actions send the data described below to Sublab 3DP; they do not send student records. O.D.I.N. does not transmit data to:

- Advertising networks
- Analytics services
- Data brokers

Enabled integrations (notification services, email, single sign-on, class roster import, smart-plug control, MQTT, SIEM streaming, Square and Spoolman) contact their configured services. Push notifications, if a user turns them on, pass the notification text, which can include a job or file name, through the browser vendor's push service, or through Apple's push service where the server operator has set that up. Opening the API documentation causes your browser to request assets from jsDelivr and FastAPI's website; ReDoc also requests Google Fonts.

License actions an administrator runs contact runsodin.com. An administrator can also ask O.D.I.N. to check whether the license server is reachable (`GET /api/system/license-server-status`, administrators only); the web app does not call it, nothing schedules it, and it sends no installation data (the portal sees the connecting IP address). Online activation sends the license key, installation ID and public device key. Starting a trial sends the installation ID, public device key and administrator-entered email address. Re-checking sends the license key, installation ID and current expiry when present, plus the public device key for device-bound paid licenses; moving a key sends the license key and installation ID. Requests also carry authentication proofs and protocol fields. Activation and move request files additionally contain the server's hostname, O.D.I.N. version and generation timestamp. The private device key is never transmitted. The portal sees the connecting IP address. These actions do not send print, job, user or student records. Activation, starting a trial and moving a key can be done without a connection from O.D.I.N., using another computer to contact the portal; re-checking requires a connection from O.D.I.N.

---

## 4. No Direct Collection from Children by Sublab 3DP

Sublab 3DP does not interact with children at any point:

- O.D.I.N. is installed by school IT staff, not by students
- User accounts are created by school administrators, by class roster import, or by single sign-on provisioning that an administrator turns on; students do not self-register with Sublab 3DP
- Sublab 3DP has no access to deployed instances
- There is no cloud service, registration portal, or website where children provide information to Sublab 3DP
- An installed license is verified locally with Ed25519 signatures, with no internet connection. Activating a license online sends the items listed in Section 3.3; it can be done by file instead

---

## 5. Parental Consent

Because the school is the operator under COPPA:

- The **school provides consent** on behalf of parents for the use of student information within O.D.I.N., per COPPA's school authorization provision
- The school should include O.D.I.N. in its technology consent disclosures to parents
- Parents retain the right to review their child's information and request deletion — the school can fulfill these requests using O.D.I.N.'s data export and erasure endpoints

Schools should verify that their existing parental consent processes cover the use of O.D.I.N. and that parents are informed about the system's data practices. The FTC requires that the school's consent be based on the operator's data collection practices.

---

## 6. Recommended Practices for Under-13 Deployments

Schools deploying O.D.I.N. for students under 13 should adopt the following practices:

### 6.1 User Account Configuration

- **Use anonymized usernames** — assign non-identifying labels such as "student_01", "maker_A3", or class-period codes instead of real names
- **Disable or omit email fields** — email is not required for O.D.I.N. to function; leave it blank for under-13 accounts
- **Configure student permissions** — Viewers can read permitted resources and submit jobs by default; configure permissions and approval requirements to enforce the school’s intended restrictions.

### 6.2 Access Controls

- **Use OIDC SSO** — integrate with the school's existing identity provider rather than managing separate credentials
- **Restrict network access** — limit O.D.I.N. to the school's internal network; do not expose it to the public internet
- **Review the audit log** — periodically check for unauthorized access or account misuse

### 6.3 Data Retention and Deletion

- **Establish a retention schedule** — delete student accounts and associated data at the end of each school year or when students leave
- **Use the GDPR erasure endpoint** (`DELETE /api/v1/users/{user_id}/erase`) to anonymize supported user data; retained operational records and excluded copies are not all deleted
- **Use the GDPR export endpoint** (`GET /api/v1/users/{user_id}/export`) to export supported user data for parental inspection requests

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
| Audit logging | Selected authentication, operational and administrative actions are logged |

Schools should deploy O.D.I.N. behind a TLS-terminating reverse proxy (nginx, Traefik, etc.) to encrypt data in transit. O.D.I.N. does not provide native TLS. This is important for protecting children's information in transit on the network.

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
- No persistent identifiers are sent for cross-site behavioral tracking; license actions send the installation identifiers described in Section 3.3
- No gamification designed to encourage excessive data sharing

---

## 10. Vendor Attestation

Sublab 3DP attests that:

1. Sublab 3DP does not collect personal information from children. The school's own O.D.I.N. installation records student account activity and submissions on the school's server; none of it is sent to Sublab 3DP
2. License actions send the data described in Section 3.3 to Sublab 3DP, but no student records through those actions. Enabled integrations, push notifications and browser requests for API-documentation assets make the connections described in Section 3.3
3. O.D.I.N. does not contain advertising or third-party tracking
4. License activation can be done entirely by file; online activation sends the items in Section 3.3
5. Sublab 3DP has no access to any deployed O.D.I.N. instance

Schools should maintain a copy of this attestation and any supplementary agreements with Sublab 3DP as part of their COPPA compliance documentation.

---

## Summary

O.D.I.N. is self-hosted software that operates entirely under the school's control. Sublab 3DP does not collect, access, or process children's personal information. Schools can configure O.D.I.N. to minimize data collection through anonymized usernames, disabled email fields, and restricted roles — supporting COPPA compliance by design.

Schools should have their legal counsel review this document and confirm that their deployment configuration and consent practices satisfy COPPA requirements before deploying O.D.I.N. for students under 13.
