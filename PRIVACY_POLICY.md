# Privacy Policy

**O.D.I.N. (Orchestrated Dispatch & Inventory Network)**

**Effective Date:** February 14, 2026
**Last Updated:** November 13, 2026

**JLLS Holdings LLC, d/b/a SubLab 3DP** ("SubLab 3DP," "Sublab 3DP," "Company," "we," "us," or "our")
**Principal:** Shane Smith
**Contact:** sublab3dp@gmail.com

---

## 1. Overview

O.D.I.N. is **self-hosted software** that runs entirely on your own infrastructure. This Privacy Policy explains:

- What data the O.D.I.N. software processes **on your servers** (Section 2)
- What data **Sublab 3DP** collects from you directly (Section 3)
- Your rights regarding your data (Section 5)

**The key point:** O.D.I.N. stores operational data on your infrastructure. License actions and enabled integrations transmit the data described in Section 2.6; we have no access to your instance unless you explicitly grant it.

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

### 2.6 Telemetry and Network Connections

**O.D.I.N. does not:**

- Send vendor telemetry or usage reports to Sublab 3DP
- Contact Sublab 3DP in the background (no heartbeat, no update check)
- Require an Internet connection to operate after initial installation
- Include analytics, tracking pixels, or third-party SDKs
- Share data with advertisers or data brokers

O.D.I.N. sends no vendor telemetry; configured integrations can transmit printer telemetry, job events and audit records. An installed license is verified locally using Ed25519 signatures, with no network call.

A default installation contacts nothing outside your network by itself. Connections the Software can make:

- **Your printers and cameras** on your local network (MQTT, HTTP, WebSocket, RTSP/MJPEG), and local network discovery (mDNS) when an administrator asks for it.
- **Our license portal**, only when an administrator runs a license action. Online activation sends the license key, installation ID and public device key. Starting a trial sends the installation ID, public device key and administrator-entered email address. Re-checking sends the license key, installation ID and current expiry when present, plus the public device key for device-bound paid licenses; moving a key sends the license key and installation ID. Requests also carry authentication proofs and protocol fields. Activation and move request files additionally contain the server's hostname, O.D.I.N. version and generation timestamp. The private device key is never transmitted. The portal sees the connecting IP address. These actions do not send print, job, user or student records. Activation, starting a trial and moving a key can be done without a connection from O.D.I.N., using another computer to contact the portal; re-checking requires a connection from O.D.I.N.
- **Services an administrator connects:** email (SMTP), notification services (webhooks, Discord, Slack, ntfy, Telegram, Pushover, WhatsApp), an MQTT broker, smart-plug control (Home Assistant, Tasmota or MQTT), SIEM streaming, a single sign-on provider, class roster import (Google Classroom, or an LTI platform such as Canvas), Square, and Spoolman if you point O.D.I.N. at one. Each is off until configured.
- **Push notifications**, only if a user turns them on: the browser vendor's push service, or Apple's push service where the server operator has set it up. The notification text, which can include a job or file name, passes through it.

- **API documentation:** Opening the API documentation causes your browser to request assets from jsDelivr and FastAPI's website; ReDoc also requests Google Fonts.

Home Assistant and dashboards can read from O.D.I.N.; when smart-plug control is configured, O.D.I.N. also calls Home Assistant, Tasmota plugs or the configured MQTT broker.

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


### 3.5 Pro Trial Requests

A 14-day Pro trial is started in O.D.I.N. (Settings > License). O.D.I.N. signs the request with your installation's device key and sends it to our license portal (or you paste it there as a request code). We collect:

- **Email address**, in cleartext on the pending request (to send the confirmation link) and on the trial license key once you confirm (as on a paid license).
- **A one-way hash (SHA-256) of the normalized email address**, kept with the trial record to enforce one trial per email address.
- **Installation ID, device public key and its fingerprint** (a SHA-256 hash of the device public key), kept with the trial record to enforce one trial per installation and to bind the trial key to that installation.
- **Your answer to "How did you hear about O.D.I.N.?"**, only if you choose to give one. It is optional, it is whatever you pick or type, and nothing is inferred automatically (no referrer, campaign tags, cookies or scripts). It is kept on the pending request and on the trial license key record.
- **The IP address the request came from.** It is kept on the pending request (deleted no later than 48 hours after the request) and on the trial record for 90 days, then removed.

We send the confirmation email through **Resend** (Resend, Inc., United States), which processes your email address and the message only to deliver it. The trial key is shown once on the confirmation page and is not emailed.

**Purpose:** Delivering the trial you asked for and preventing abuse of the one-trial limit. The optional answer is used only to understand where people find O.D.I.N.
**Legal basis (GDPR):** Steps taken at your request before a contract (Article 6(1)(b)) and our legitimate interest in preventing abuse of free trials (Article 6(1)(f)).
**Retention:** Pending requests (including the optional answer on them): no more than 48 hours. The optional answer on the trial license key record: as for license records. Trial records (email hash, installation ID, device key fingerprint): as long as needed to enforce one trial per installation and per email address. Request IP on the trial record: 90 days. Trial license key records: as for license records.

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

- **GDPR data export:** Full JSON export of all user data (all tiers)
- **Anonymization:** Anonymize user records while preserving analytics (all tiers)
- **Data retention policies:** Configurable TTL per data type (all tiers)
- **Audit log:** Searchable local history of the audited activity described below.


ODIN keeps an audit log on your own server. It records sign-ins (password and SSO, successful and failed), file uploads, downloads of design files, backups and timelapses, exports, print submissions and dispatch, watching a printer's live camera or a timelapse, and administrative changes. Each entry holds the time, what was done and to what, and, where known, the user and the IP address they connected from. Watching a camera or timelapse is recorded when it starts and then at most once every 15 minutes per person, not frame by frame. On Education installations this includes students' activity. The log isn't a record of every request. Audit entries are stored on your server and follow your retention settings; if SIEM streaming is enabled, copies are sent to the configured destination. When a user is erased, ODIN removes their name and IP address from their audit entries, including SSO sign-ins. The entries themselves remain, with what was done, and SSO sign-in entries keep the user's ID number.

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
- Anonymize user records (all tiers)

No request to Sublab 3DP is needed — you control the server.

### 5.3 Supervisory Authority

If you are located in the European Economic Area, you have the right to lodge a complaint with your local data protection supervisory authority.

---

## 6. Children's Privacy

### 6.1 General Use

O.D.I.N. is not directed at children under 13. We do not knowingly collect personal information from children under 13 through our website or license issuance process.

### 6.2 Educational Use

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

When administrators run term cleanup, ODIN deletes eligible uploaded print files and attributable media according to the school's selected retention policy. Completed graduation erasure disables eligible student accounts and removes or replaces supported account identifiers, leaving numbered school records. Personal information may remain in filenames, titles, notes, review comments and other retained text. Cleanup does not reach backups, downloaded or exported copies, printer storage, camera detection frames or timelapses ODIN cannot attribute to a submission. See [term retention](docs/education/term-retention.md) for policy choices, retained records, exclusions and handling of blocked or incomplete cleanup.

By default, O.D.I.N. runs on the school's own infrastructure and stores student data only in that deployment's database and file storage; it sends no telemetry or usage data to Sublab 3DP or any third party (Section 2.6); and administrators can export a user's data, anonymize user records and set data retention periods (Section 4.3). These are product behaviors, not a FERPA, COPPA or other compliance certification: self-hosting alone does not make a deployment compliant. Whether a deployment meets those obligations depends on how the school configures, operates and governs it, and remains the school's responsibility.

---

## 7. International Data Transfers

### 7.1 Self-Hosted Software

Local operation keeps data where you deploy O.D.I.N.; license actions, enabled integrations, push and browser requests described in Section 2.6 may involve international transfers.

### 7.2 License and Support Data

Sublab 3DP is based in the United States. If you are located outside the United States, the limited data we collect (license information, Pro trial requests, support interactions) may be stored and processed in the United States. For EEA residents, this transfer is based on:

- Standard Contractual Clauses (SCCs) where required
- Our legitimate interest in providing the contracted services

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
- IP allowlisting (all tiers)
- MFA / TOTP support (all tiers)
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
| Pending Pro trial requests (email, email hash, installation ID, device key, IP, optional "how did you hear" answer) | No more than 48 hours |
| Pro trial records (email hash, installation ID, device key fingerprint) | As long as needed to enforce one trial per installation and email |
| IP address on a Pro trial record | 90 days |
| Support correspondence | 3 years after last interaction |
| Website analytics (aggregate) | 2 years |
| Payment records | As required by tax law (typically 7 years) |

### 9.2 Data on Your Instance

You control retention for all data on your O.D.I.N. instance. The Software includes configurable data retention policies (all tiers) and manual cleanup tools. Default retention periods within the Software:

- Telemetry snapshots: 90 days
- HMS error history: 90 days
- AMS environment data: 90 days
- Audit logs, jobs, and other records: indefinite (until you delete them)

---

## 10. Third-Party Services

The O.D.I.N. software does not include third-party analytics, advertising, or tracking services. Third-party connections are described in Section 2.6. Examples of configured or local connections include:

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

**JLLS Holdings LLC, d/b/a SubLab 3DP**
Shane Smith, Principal
Email: sublab3dp@gmail.com

We aim to respond to all privacy inquiries within 30 days.
