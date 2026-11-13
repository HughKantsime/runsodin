# Terms of Service & End User License Agreement

**O.D.I.N. (Orchestrated Dispatch & Inventory Network)**

**Effective Date:** February 14, 2026
**Last Updated:** November 13, 2026 <!-- release-date placeholder: set by ops/bump-version.sh -->

**JLLS Holdings LLC, d/b/a SubLab 3DP** ("SubLab 3DP," "Sublab 3DP," "Company," "we," "us," or "our")
**Principal:** Shane Smith
**Contact:** support@runsodin.com

---

## 1. Acceptance of Terms

By downloading, installing, accessing, or using O.D.I.N. software ("Software"), you ("Customer," "you," or "your") agree to be bound by these Terms of Service and End User License Agreement ("Agreement"). If you are accepting on behalf of an organization, you represent that you have authority to bind that organization.

If you do not agree to these terms, do not download, install, or use the Software.

---

## 2. Definitions

- **"Software"** means the O.D.I.N. application, including all backend services, frontend interfaces, monitor daemons, Vigil AI components, documentation, and updates provided under this Agreement.
- **"License Key"** means the Ed25519-signed license file that activates a specific tier of functionality.
- **"Community Tier"** means the free, limited edition of the Software (5 printers, 1 user).
- **"Pro Tier"** means the paid commercial license at $15/month or $150/year.
- **"Education Tier"** means the paid license, licensed per site and quoted on request, designed for educational institutions.
- **"Enterprise Tier"** means the paid license, priced per agreement, which adds the Enterprise features described in Section 3.4.
- **"Instance"** means a single deployment of the Software on a single server or Docker host.
- **"Site"** means one physical location of one legal entity, or one environment controlled by one legal entity (for example one cloud tenant or one enclave). When a service provider operates the Software for several customers, each customer's location or environment is a separate Site.
- **"Vigil AI"** means the optional AI-based print failure detection system included in the Software.

---

## 3. License Grant

### 3.1 Community Tier (Free)

Subject to this Agreement, we grant you a non-exclusive, non-transferable, revocable license to download, install, and use the Software for **non-commercial and personal purposes only**, limited to 5 printers and 1 user per Instance. The Community Tier may not be used for commercial production, revenue-generating activities, or any business operations.

### 3.2 Pro Tier

Subject to payment of applicable fees and this Agreement, we grant you a non-exclusive, non-transferable license to install and use the Software on **one (1) Instance** for commercial purposes, with unlimited printers and users. The Pro Tier includes all features listed in the Pro feature set as documented at the time of purchase.

### 3.3 Education Tier

Subject to payment of applicable fees and this Agreement, we grant you a non-exclusive, non-transferable license to install and use the Software on **one (1) Instance per licensed site** for educational purposes. The Education Tier includes all Pro features plus job approval workflows, user groups and print quotas, with unlimited printers and users.

### 3.4 Enterprise Tier

Subject to payment of applicable fees and this Agreement, we grant you a non-exclusive, non-transferable license to install and use the Software on **up to three (3) Instances per licensed Site** for any lawful purpose: one production Instance and up to two non-production Instances for staging, testing or disaster recovery, unless your agreement states otherwise. The Enterprise Tier includes all Education features plus a tamper-evident audit log (integrity verification, signed export, CSV export and streaming to a SIEM), part traceability (inspections and signed certificates of conformance), controlled-part handling for CUI, ITAR and custom markings, and MQTT event republishing, with unlimited printers and users. Features are as documented for the version you run. No feature of the Software is a certification, authorization or attestation under CMMC, FedRAMP or any other program. MFA, IP allowlisting and GDPR data export/anonymization are included in every tier, including the Community Tier. Organization management is included in the Education and Enterprise Tiers.

### 3.5 License Restrictions

You may NOT:

- Sublicense, rent, lease, or lend the Software or License Key to any third party
- Offer the Software to third parties as a hosted, multi-tenant or software-as-a-service product, or run one Instance for more than one customer, except as a service provider under Section 3.7
- Share, distribute, or publish your License Key
- Circumvent, disable, or tamper with the license validation mechanism
- Remove or alter any proprietary notices, labels, or marks
- Use the Community Tier for commercial purposes

These restrictions apply to O.D.I.N.'s own code. Third-party components that ship with the Software under their own licenses, listed in `THIRD_PARTY_NOTICES.md` (including the bundled Obico spaghetti-detection model, licensed under AGPL-3.0), are licensed to you under those licenses. Nothing in this Agreement restricts the rights those licenses give you.

### 3.6 BSL 1.1 Incorporation

The Software is licensed under the Business Source License 1.1. The full text of the BSL 1.1 is included with the Software in the LICENSE file. In the event of a conflict between this Agreement and the BSL 1.1, the more restrictive provision applies. Neither this Agreement nor the BSL 1.1 applies to the separately licensed third-party components described in Section 3.5; those components are governed only by their own licenses. Per the BSL 1.1, each version of the Software will convert to the Apache License 2.0 on the earlier of (a) the Change Date specified in the LICENSE file or (b) four years after the first public release of that version.

### 3.7 Service Providers and Authorized Resellers

**Service providers.** A managed service provider, integrator or contractor may install, configure, operate and support the Software for a customer if: (a) the customer holds a valid license for each Instance; (b) each Instance runs on the customer's own infrastructure or in an environment dedicated solely to that customer; and (c) no Instance serves more than one customer. The customer remains the licensee and is responsible for its service provider's compliance with this Agreement.

**Authorized resellers.** We may appoint resellers in writing. A License Key bought through an authorized reseller is licensed by JLLS Holdings LLC directly to the end customer under this Agreement; the reseller does not sublicense the Software. Reseller pricing and terms are set in a separate written agreement.

---

## 4. Account Terms

### 4.1 Registration

Paid tiers require providing accurate contact information for license issuance. You are responsible for maintaining the security of your License Key and administrative credentials.

### 4.2 Account Security

You are responsible for all activity under your Instance, including actions taken by users you provision within the Software. You must promptly notify us of any unauthorized use at support@runsodin.com.

### 4.3 Self-Hosted Responsibility

The Software is self-hosted on your infrastructure. You are solely responsible for:

- Server security, access controls, and network configuration
- Backups of your database and configuration
- Operating system and Docker environment updates
- Ensuring adequate hardware resources

---

## 5. Payment Terms

### 5.1 Pricing

Current pricing is published on our website. All prices are in US Dollars (USD) unless otherwise stated.

| Tier | Price |
|------|-------|
| Community | Free |
| Pro | $15/month or $150/year |
| Education | Quoted per site (contact us) |
| Enterprise | Quoted per agreement (contact sales) |

### 5.2 Subscription and Auto-Renewal

The Pro Tier is a subscription, billed monthly or annually as offered at checkout. It is charged when you subscribe and at the start of each billing period, and it renews automatically for successive periods of the same length at the then-current price until you cancel. You can cancel at any time from your subscription account, linked from runsodin.com/manage-subscription and from your receipt. Cancellation takes effect at the end of the period you have paid for, and you keep your license until then. We email a reminder before each annual renewal.

### 5.3 Orders, Seller and Payment

Online orders for paid licenses are processed by Paddle.com Market Limited and its affiliates ("Paddle"), our merchant of record and reseller. Paddle is the seller of record for your order, collects and remits applicable taxes, processes payment, and provides billing support, and Paddle's buyer terms apply to your purchase. We license the Software to you under this Agreement and provide support under Section 8. Education and Enterprise licenses are quoted, and may be invoiced with payment terms set out in your quote or agreement.

### 5.4 Taxes

Applicable taxes are calculated and shown at checkout by Paddle. For licenses invoiced directly by us, prices exclude applicable taxes, which are shown on the invoice.

### 5.5 Price Changes

We may change pricing with 60 days' written notice. Price changes take effect at the start of your next billing period. Your continued use after the new billing period begins constitutes acceptance.

### 5.6 Licenses Purchased Before Online Checkout

Licenses purchased by invoice before online checkout was available keep the term stated on their invoice. They can be renewed through online checkout or by a new invoice.

### 5.7 Refunds

You can try the Pro Tier free for 14 days before you subscribe. Payments are non-refundable, except where applicable law or Paddle's buyer terms give you a right to a refund. See our Refund Policy at runsodin.com/refund-policy. Paddle handles refunds for online orders.

---

## 6. Acceptable Use

You agree not to:

- Use the Software for any unlawful purpose
- Attempt to reverse-engineer the License Key signing mechanism
- Interfere with or disrupt the license validation infrastructure
- Use the Software to develop a competing product (during the BSL 1.1 license period)
- Misrepresent your tier or license status
- Use the Community Tier in a commercial or revenue-generating capacity

---

## 7. Vigil AI Terms

### 7.1 Not a Safety Device

Vigil AI is a **quality monitoring tool**, not a safety device. It is designed to detect certain print failure patterns (spaghetti, first layer defects, detachment) and optionally pause prints. **It must not be relied upon as the sole means of monitoring 3D printers.**

### 7.2 No Safety Guarantees

We make no guarantees regarding the accuracy, reliability, or completeness of Vigil AI detections. False negatives (missed failures) and false positives (incorrect detections) are expected and inherent to the technology.

### 7.3 Assumption of Risk

You acknowledge that 3D printing involves inherent risks including fire, equipment damage, and injury. By using Vigil AI, you accept full responsibility for printer operation and safety. See the Vigil AI Disclaimer document for detailed safety information.

---

## 8. Support

### 8.1 Community Tier

Community support only. Access to community forums and GitHub issues. No guaranteed response time. No direct support from Sublab 3DP.

### 8.2 Pro Tier

Email support at support@runsodin.com. Best-effort response within 3 business days. Support covers installation, configuration, and usage questions. Does not include custom development, hardware troubleshooting, or network configuration.

### 8.3 Education Tier

Email support with best-effort response within 2 business days. Includes onboarding assistance (one 30-minute remote session per site). Academic-year-aligned support scheduling available.

### 8.4 Enterprise Tier

Priority email support with best-effort response within 1 business day. Includes dedicated onboarding, quarterly check-in calls, and priority bug fix consideration.

### 8.5 No SLA

**The Software is self-hosted. We do not provide uptime guarantees, service level agreements (SLAs), or availability commitments.** System availability depends entirely on your infrastructure and administration.

---

## 9. Intellectual Property

### 9.1 Ownership

The Software, including all code, documentation, designs, and trademarks (O.D.I.N., Vigil AI, Sublab 3DP), is and remains the property of JLLS Holdings LLC, d/b/a SubLab 3DP. This Agreement grants a license to use the Software; it does not transfer ownership.

### 9.2 Your Data

You retain all rights to data you create, store, or process using the Software. We claim no ownership of your printer data, models, job history, customer information, or any other data stored in your Instance.

### 9.3 Feedback

If you provide suggestions, feature requests, or other feedback, we may use that feedback without obligation to you.

---

## 10. Warranty Disclaimer

**THE SOFTWARE IS PROVIDED "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT.**

We do not warrant that:

- The Software will meet your specific requirements
- The Software will operate uninterrupted, timely, secure, or error-free
- Results obtained from the Software will be accurate or reliable
- Vigil AI will detect all print failures or prevent damage
- The Software will be compatible with all printer hardware or firmware versions

---

## 11. Limitation of Liability

### 11.1 Liability Cap

**TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, OUR TOTAL AGGREGATE LIABILITY ARISING OUT OF OR RELATED TO THIS AGREEMENT SHALL NOT EXCEED THE TOTAL FEES PAID BY YOU TO US IN THE TWELVE (12) MONTHS IMMEDIATELY PRECEDING THE EVENT GIVING RISE TO THE CLAIM.**

**FOR COMMUNITY TIER (FREE) USERS, OUR TOTAL AGGREGATE LIABILITY SHALL NOT EXCEED ZERO DOLLARS ($0).**

### 11.2 Exclusion of Damages

**IN NO EVENT SHALL WE BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING BUT NOT LIMITED TO:**

- Loss of profits, revenue, or data
- Business interruption
- Printer damage, fire, or equipment failure
- Property damage or personal injury arising from printer operation
- Failed or missed print failure detections by Vigil AI
- Cost of procurement of substitute goods or services

**THESE LIMITATIONS APPLY REGARDLESS OF THE THEORY OF LIABILITY (CONTRACT, TORT, NEGLIGENCE, STRICT LIABILITY, OR OTHERWISE) AND EVEN IF WE HAVE BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.**

### 11.3 Essential Purpose

The limitations in this section apply even if any limited remedy fails of its essential purpose.

---

## 12. Indemnification

You agree to indemnify, defend, and hold harmless Sublab 3DP, Shane Smith, and their affiliates from any claims, damages, losses, or expenses (including reasonable attorneys' fees) arising from:

- Your use of the Software
- Your violation of this Agreement
- Your violation of any applicable law or regulation
- Any data processed or stored by your Instance
- Any injury, damage, or loss resulting from your printer operations

---

## 13. Data Responsibility

### 13.1 Self-Hosted Architecture

O.D.I.N. is self-hosted software. All data — including printer telemetry, job records, user accounts, models, camera frames, and AI detection data — resides exclusively on your infrastructure. **Sublab 3DP does not host, access, process, or store your operational data.**

### 13.2 Data Controller

You are the sole data controller for all data processed by your Instance. You are responsible for compliance with all applicable data protection laws (GDPR, CCPA, FERPA, etc.) as they relate to data stored in your Instance.

### 13.3 No Data Processing Agreement Required

Because we do not process your data, a Data Processing Agreement (DPA) is generally not required for standard use. If you grant us access to your Instance for support purposes, a DPA may be appropriate — contact us to arrange one.

---

## 14. Termination

### 14.1 Termination by You

You may stop using the Software at any time by uninstalling it and destroying your License Key. For paid tiers, cancellation of your subscription does not entitle you to a refund of fees already paid, except as described in Section 5.7.

### 14.2 Termination by Us

We may terminate or suspend your license immediately if you:

- Violate this Agreement
- Fail to pay applicable fees within 30 days of the due date
- Use the Software in a manner that violates applicable law
- Share, distribute, or publish your License Key
- Offer the Software as a hosted service in breach of Section 3.5

### 14.3 Effect of Termination

Upon termination:

- Your License Key will be revoked and will no longer validate
- You must cease all use of the Software under the terminated license
- You may retain your data (the Software does not delete your database on license expiry)
- The Community Tier remains available under its terms
- Sections 9, 10, 11, 12, 13, 15, and 16 survive termination

---

## 15. General Provisions

### 15.1 Governing Law

This Agreement is governed by the laws of the United States and the State of Delaware, without regard to conflict of law principles. Any disputes arising under this Agreement shall be resolved in the state or federal courts located in New Castle County, Delaware.

### 15.2 Export Compliance

The Software may include encryption technology subject to U.S. export control laws (EAR). You agree to comply with all applicable export and re-export restrictions. You represent that you are not located in, or a national or resident of, any country subject to U.S. trade sanctions (currently Cuba, Iran, North Korea, Syria, and the Crimea, Donetsk, and Luhansk regions of Ukraine), and that you are not on any U.S. government restricted party list.

### 15.3 Modifications to Terms

We may modify these terms by providing 30 days' written notice via email to the address associated with your license. Your continued use of the Software after the 30-day notice period constitutes acceptance of the modified terms. If you do not agree to the modified terms, you must cease using the Software before the changes take effect.

Material changes to pricing or license scope will not apply until your next renewal period.

### 15.4 Assignment

You may not assign or transfer this Agreement without our prior written consent. We may assign this Agreement in connection with a merger, acquisition, or sale of substantially all of our assets.

### 15.5 Severability

If any provision of this Agreement is held unenforceable, the remaining provisions remain in full effect. The unenforceable provision will be modified to the minimum extent necessary to make it enforceable.

### 15.6 Entire Agreement

This Agreement, together with the BSL 1.1 license and any applicable order form, constitutes the entire agreement between you and Sublab 3DP regarding the Software and supersedes all prior agreements and understandings.

### 15.7 Waiver

Failure to enforce any provision of this Agreement does not constitute a waiver of that provision or any other provision.

### 15.8 Force Majeure

Neither party shall be liable for delays or failures in performance resulting from causes beyond its reasonable control, including natural disasters, war, terrorism, pandemics, government actions, or Internet disruptions.

---

## 16. Contact Information

**JLLS Holdings LLC, d/b/a SubLab 3DP**
Shane Smith, Principal
Email: support@runsodin.com

For license inquiries, support, or legal questions, contact us at support@runsodin.com.
