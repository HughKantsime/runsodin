# Privacy Policy

**O.D.I.N. (Orchestrated Dispatch & Inventory Network)**

**Effective Date:** February 14, 2026
**Last Updated:** February 14, 2026

**Sublab 3DP** ("Company," "we," "us," or "our")
**Principal:** Shane Smith
**Contact:** sublab3dp@gmail.com

---

## 1. Overview

O.D.I.N. is **self-hosted software** that runs entirely on your own infrastructure. This Privacy Policy explains:

- What data the O.D.I.N. software processes **on your servers** (Section 2)
- What data **Sublab 3DP** collects from you directly (Section 3)
- Your rights regarding your data (Section 5)

**The key point:** When you run O.D.I.N., your data stays on your hardware. We do not operate cloud services, do not host customer data, and have no access to your O.D.I.N. instance unless you explicitly grant it.

---

## 2. Data Processed by the O.D.I.N. Software (On Your Infrastructure)

The following data is created and stored by the O.D.I.N. software on your server. **Sublab 3DP does not have access to any of this data.**

### 2.1 Printer Telemetry

- Printer names, IP addresses, serial numbers, and connection credentials (encrypted at rest with Fernet)
- Temperature readings (nozzle, bed, enclosure), fan speeds, print progress
- Printer status and error history (e.g., Bambu HMS error codes)
- AMS slot data (filament type, color, humidity)

### 2.2 Job and Print Data

- Print job records (model name, status, start/end time, assigned printer)
- 3D model files (.3mf) uploaded to the model library
- Print time estimates and actual durations
- Failure logs and failure reasons

### 2.3 User Accounts

- Usernames, email addresses, hashed passwords (bcrypt)
- Role assignments (admin, operator, viewer)
- MFA secrets (TOTP, encrypted)
- Session tokens, login history, IP addresses of login attempts
- Print quotas and usage data

### 2.4 Camera and Vision Data

- Camera stream URLs and configurations
- Vigil AI detection frames (JPEG images stored locally)
- Detection metadata (confidence scores, detection type, timestamps)
- Timelapse frames and compiled video files

### 2.5 Business Data

- Product catalog, bills of materials, orders, invoices
- Customer names and order references (as entered by you)
- Spool inventory and consumable stock levels
- Cost and revenue analytics

### 2.6 No Telemetry, No Phone-Home

**O.D.I.N. does not:**

- Send telemetry or usage data to Sublab 3DP or any third party
- Phone home to verify licenses (license validation is performed locally using Ed25519 signatures)
- Require an Internet connection to operate after initial installation
- Include analytics, tracking pixels, or third-party SDKs
- Share data with advertisers or data brokers

The only network connections the Software makes are:

- To your printers on your local network (MQTT, HTTP, WebSocket)
- To your configured notification channels (email SMTP, Discord/Slack webhooks, Telegram, ntfy) — only if you configure them
- To your configured MQTT broker — only if you enable MQTT republish

---

## 3. Data Collected by Sublab 3DP

We collect limited data through the following interactions:

### 3.1 License Issuance

When you purchase a license, we collect:

- Your name or organization name
- Email address
- License tier and expiration date
- Payment information (processed by our payment processor; we do not store full credit card numbers)

**Purpose:** License issuance, account management, renewal notifications.
**Legal basis (GDPR):** Performance of a contract (Article 6(1)(b)).
**Retention:** For the duration of your license plus 7 years for tax/accounting purposes.

### 3.2 Support Interactions

When you contact us for support, we collect:

- Your name and email address
- The content of your support request
- Any diagnostic information you voluntarily share (logs, screenshots, configuration)

**Purpose:** Providing technical support.
**Legal basis (GDPR):** Performance of a contract (Article 6(1)(b)) for paid tiers; legitimate interest (Article 6(1)(f)) for Community tier.
**Retention:** 3 years after last interaction, or until you request deletion.

### 3.3 Marketing Website

Our marketing website (separate from the O.D.I.N. software) may use:

- **Analytics:** Privacy-respecting analytics (no personal data tracking). We may use cookie-free analytics services that collect aggregate page view data, referral sources, and browser/device type. No personal identifiers are collected.
- **Contact forms:** If you submit a contact form, we collect the information you provide (name, email, message).

**Purpose:** Understanding website traffic, responding to inquiries.
**Legal basis (GDPR):** Legitimate interest (Article 6(1)(f)).

### 3.4 Cookies

The O.D.I.N. software uses **localStorage** for user preferences (theme, language) — these are not cookies and are never transmitted to us.

Our marketing website may use:

- **Essential cookies:** Session management for contact forms (if applicable)
- **No tracking cookies:** We do not use advertising, retargeting, or third-party tracking cookies

[ATTORNEY REVIEW RECOMMENDED: If any analytics service is added to the marketing site that uses cookies, a cookie consent banner will be required under GDPR/ePrivacy Directive.]

---

## 4. Data Processing Relationship

### 4.1 You Are the Data Controller

For all data processed by your O.D.I.N. instance, **you are the data controller** under GDPR and equivalent data protection laws. You determine what data is collected, how it is used, how long it is retained, and who has access.

### 4.2 Sublab 3DP Is NOT a Data Processor

Because we do not access, host, or process data on your instance, **Sublab 3DP is not a data processor** for your O.D.I.N. data. A Data Processing Agreement (DPA) is therefore not required for standard use.

**Exception:** If you grant us remote access to your instance for support or troubleshooting, we may temporarily act as a data processor for the duration of that access. In such cases, contact us to establish appropriate terms.

### 4.3 Your Responsibilities as Data Controller

If you store personal data in O.D.I.N. (e.g., user accounts, customer names in orders), you are responsible for:

- Complying with applicable data protection laws (GDPR, CCPA, FERPA, etc.)
- Providing privacy notices to your users and customers
- Responding to data subject access requests
- Configuring appropriate data retention policies within the Software
- Securing your instance and managing user access

O.D.I.N. includes tools to assist with data protection compliance:

- **GDPR data export:** Full JSON export of all user data (Enterprise tier)
- **Anonymization:** Anonymize user records while preserving analytics (Enterprise tier)
- **Data retention policies:** Configurable TTL per data type (Enterprise tier)
- **Audit log:** Searchable history of administrative actions

---

## 5. Your Rights (GDPR and Other Applicable Laws)

### 5.1 Rights Regarding Data We Hold

For personal data that Sublab 3DP holds directly (license information, support interactions), you have the right to:

- **Access:** Request a copy of your personal data
- **Rectification:** Request correction of inaccurate data
- **Erasure:** Request deletion of your data (subject to legal retention obligations)
- **Restriction:** Request that we limit processing of your data
- **Portability:** Receive your data in a structured, machine-readable format
- **Object:** Object to processing based on legitimate interest
- **Withdraw consent:** Where processing is based on consent

To exercise any of these rights, contact us at **sublab3dp@gmail.com**. We will respond within 30 days.

### 5.2 Rights Regarding Data on Your Instance

For data stored on your O.D.I.N. instance, you have full control. You can:

- Export all data at any time (database backups, CSV exports, GDPR data export)
- Delete any data at any time
- Uninstall the Software and retain your database
- Anonymize user records (Enterprise tier)

No request to Sublab 3DP is needed — you control the server.

### 5.3 Supervisory Authority

If you are located in the European Economic Area, you have the right to lodge a complaint with your local data protection supervisory authority.

[ATTORNEY REVIEW RECOMMENDED: Identify the lead supervisory authority if the company has EU customers. Consider whether an EU representative is needed under GDPR Article 27.]

---

## 6. Children's Privacy

### 6.1 General Use

O.D.I.N. is not directed at children under 13. We do not knowingly collect personal information from children under 13 through our website or license issuance process.

### 6.2 Educational Use (COPPA Compliance)

When O.D.I.N. is used in educational settings (K-12 schools) under the Education Tier:

- The school or educational institution acts as the data controller and provides consent on behalf of students under COPPA
- Student data is stored entirely on the school's own infrastructure — Sublab 3DP does not receive or process student data
- The school is responsible for obtaining appropriate parental consent where required by COPPA or applicable state law
- Student accounts within O.D.I.N. are managed by the school's administrators

**We recommend that schools using O.D.I.N.:**

- Use generic usernames rather than students' full names where possible
- Configure the viewer role for student accounts (minimal data exposure)
- Enable print quotas to manage student usage
- Review the Software's data retention settings

Because O.D.I.N. is self-hosted and no student data is transmitted to Sublab 3DP, the Software is designed to support FERPA and COPPA compliance by default. However, the school remains responsible for its own compliance obligations.

[ATTORNEY REVIEW RECOMMENDED: COPPA and FERPA compliance should be validated by counsel familiar with education privacy law, particularly regarding the school consent exception under COPPA.]

---

## 7. International Data Transfers

### 7.1 Self-Hosted Software

Because O.D.I.N. runs on your infrastructure, no international data transfer occurs as a result of using the Software. Your data remains wherever you deploy it.

### 7.2 License and Support Data

Sublab 3DP is based in the United States. If you are located outside the United States, the limited data we collect (license information, support interactions) may be stored and processed in the United States. For EEA residents, this transfer is based on:

- Standard Contractual Clauses (SCCs) where required
- Our legitimate interest in providing the contracted services

[ATTORNEY REVIEW RECOMMENDED: If serving EU customers, consider implementing Standard Contractual Clauses or confirming applicability of a transfer mechanism.]

---

## 8. Data Security

### 8.1 Our Infrastructure

We protect the limited data we hold (license records, support emails) using reasonable administrative, technical, and physical safeguards, including encrypted storage and access controls.

### 8.2 Your Instance

Security of your O.D.I.N. instance is your responsibility. The Software includes security features to assist you:

- Fernet encryption for printer credentials at rest
- Bcrypt password hashing
- JWT-based authentication with configurable session management
- RBAC with three role levels (admin, operator, viewer)
- IP allowlisting (Enterprise tier)
- MFA / TOTP support (Enterprise tier)
- Rate limiting on authentication endpoints
- Account lockout after failed login attempts
- Audit logging

We strongly recommend:

- Running O.D.I.N. behind a reverse proxy with TLS (HTTPS)
- Keeping Docker and your host operating system updated
- Using strong administrator passwords
- Regularly backing up your database
- Restricting network access to your instance

---

## 9. Data Retention

### 9.1 Data We Hold

| Data Type | Retention Period |
|-----------|-----------------|
| License records | Duration of license + 7 years |
| Support correspondence | 3 years after last interaction |
| Website analytics (aggregate) | 2 years |
| Payment records | As required by tax law (typically 7 years) |

### 9.2 Data on Your Instance

You control retention for all data on your O.D.I.N. instance. The Software includes configurable data retention policies (Enterprise tier) and manual cleanup tools. Default retention periods within the Software:

- Telemetry snapshots: 90 days
- HMS error history: 90 days
- AMS environment data: 7 days
- Audit logs, jobs, and other records: indefinite (until you delete them)

---

## 10. Third-Party Services

The O.D.I.N. software does not include third-party analytics, advertising, or tracking services. The only third-party connections are those you configure:

- Printer protocols (connections to your printers)
- Notification channels (SMTP, Discord, Slack, Telegram, ntfy — only if configured by you)
- OIDC/SSO provider (e.g., Microsoft Entra ID — only if configured by you)
- MQTT broker (only if you enable MQTT republish)
- go2rtc for camera streaming (bundled, runs locally)

---

## 11. Changes to This Privacy Policy

We may update this Privacy Policy from time to time. We will provide notice of material changes by:

- Posting the updated policy with a new "Last Updated" date
- Emailing license holders at their registered email address for material changes

Your continued use of the Software or our services after such notice constitutes acceptance of the updated policy.

---

## 12. Contact Us

For privacy-related questions, data subject requests, or concerns:

**Sublab 3DP**
Shane Smith, Principal
Email: sublab3dp@gmail.com

We aim to respond to all privacy inquiries within 30 days.

---

*[ATTORNEY REVIEW RECOMMENDED: This Privacy Policy should be reviewed by a qualified attorney, particularly regarding GDPR compliance (EU representative requirement, DPIA obligations, transfer mechanisms), COPPA/FERPA compliance for educational use, and CCPA requirements if California consumers are served.]*

*This document is provided as a template and does not constitute legal advice.*
