# O.D.I.N. — Go-to-Market Strategy

## Product Summary

**O.D.I.N.** (Orchestrated Dispatch & Inventory Network) is a self-hosted 3D print farm management platform. Single Docker container, runs on any Linux box or NAS. Monitors printers across 4 protocols (Bambu MQTT, Klipper/Moonraker, PrusaLink, Elegoo SDCP), schedules jobs with color-match scoring, tracks filament inventory, detects print failures with on-device AI, and manages orders with profitability tracking.

**Deployment model:** Self-hosted only. One `docker compose up` command. No cloud dependency, no telemetry, no account required. Air-gap friendly.

**Revenue model:** Tiered licenses — free Community edition with paid Pro/Education/Enterprise tiers via Ed25519 signed license files (no phone home).

---

## Tagline

"One Dashboard for Every Printer."

---

## The Problem

Running more than 2-3 printers is chaos. The tools that exist are either:

1. **Manufacturer apps** (Bambu Studio, PrusaSlicer) — one brand only, no fleet view, no scheduling, no inventory. You end up with 4 browser tabs open.
2. **OctoPrint** — single-printer focused, requires a Raspberry Pi per printer, no native multi-printer fleet management. Plugins exist but fragile.
3. **Mainsail/Fluidd** — Klipper only. Great for one printer, useless if you also have Bambu or Prusa machines.
4. **SimplyPrint** — cloud-hosted, subscription pricing, data leaves your network, limited printer protocol support, can't run air-gapped.
5. **3DPrinterOS** — cloud-only, enterprise pricing ($$$), requires per-printer agents, vendor lock-in.
6. **Spreadsheets and whiteboards** — the actual reality for most small farms. Manual tracking, no automation, no alerts.

**No self-hosted solution manages printers across all major protocols from a single dashboard.** If you have a Bambu X1C, a Prusa MK4, and two Klipper machines, you're juggling 3 different interfaces with no unified queue, no shared inventory, and no cross-fleet analytics.

---

## Target Customer Persona

### Primary: Small Print Farm Operators (3-20 printers)
- **Who:** Side-hustle and small-business 3D print farm owners. Running Etsy shops, local manufacturing, or contract printing. Own a mix of printer brands. Technical enough to run Docker.
- **Where they hang out:** r/3Dprinting, r/BambuLab, r/prusa3d, r/klippers, r/3dprintingbusiness, Facebook groups (3D Printing Business, Bambu Lab Community), Discord servers (Bambu, Voron, 3D printing business)
- **What they search for:** "3d print farm management software," "manage multiple 3d printers," "bambu farm software," "print queue for multiple printers," "3d printing business software"
- **Pain:** Wasting time babysitting printers, losing track of which spool is on which machine, no idea which jobs are profitable

### Secondary: Makerspaces and Fab Labs (5-50 printers)
- **Who:** Community workshops, library makerspaces, coworking spaces with 3D printing stations. Need user management, job approval, and accountability.
- **Where they hang out:** r/makerspaces, Maker Faire communities, library innovation lab networks, STEM education forums
- **What they search for:** "makerspace printer management," "3d printer queue system," "print job approval workflow"
- **Pain:** Students/members submitting bad prints, no accountability, filament waste, can't track who printed what

### Tertiary: School STEM Labs and Universities (5-30 printers)
- **Who:** K-12 STEM teachers, university engineering departments, community college fabrication labs. Need approval workflows, quotas, and departmental billing.
- **Where they hang out:** ISTE community, r/STEM, education technology conferences, maker education networks
- **What they search for:** "school 3d printer management," "student print quota system," "3d printing lab software education"
- **Pain:** Students wasting filament, no approval process, can't charge back to departments, need audit trail for equipment usage

### Quaternary: Enterprise Manufacturing (20-100+ printers)
- **Who:** In-house rapid prototyping labs, medical device companies, aerospace shops using additive manufacturing. Need compliance, SSO, and multi-tenancy.
- **Where they hang out:** Additive Manufacturing Users Group (AMUG), SME conferences, LinkedIn additive manufacturing groups
- **What they search for:** "additive manufacturing MES," "3d print farm enterprise software," "self-hosted manufacturing execution system"
- **Pain:** Regulatory compliance (audit trails, data residency), IT security requirements (air-gap, SSO), department-level cost allocation

---

## Value Proposition

**For small farms (Community tier):**
Free, self-hosted dashboard that monitors up to 5 printers across any brand from a single screen. Real-time telemetry, job queue, filament inventory, camera feeds, alerts via Discord/Slack/Telegram/ntfy. Install in 5 minutes, runs on a $35 mini PC.

**For growing farms (Pro tier):**
Unlimited printers and users. AI failure detection (Vigil) catches spaghetti and first-layer issues before they waste a whole spool. Orders, products, and BOM tracking with per-job profitability. White-label branding. Analytics and cost calculator. Timelapse generation.

**For education (Education tier):**
Everything in Pro plus job approval workflows (students submit, instructors approve), per-student print quotas, departmental chargebacks, usage reports, and OIDC/SSO integration with campus identity providers.

**For enterprise (Enterprise tier):**
Everything in Pro plus MFA, IP allowlisting, multi-tenant organizations, GDPR data export/erasure, FERPA/COPPA compliance documentation, and full audit logging.

**Universal:**
- Self-hosted — your data never leaves your network
- Air-gap friendly — works without internet after install
- Protocol agnostic — Bambu, Klipper, Prusa, Elegoo in one dashboard
- Single Docker container — no microservice sprawl, no Kubernetes needed
- BSL 1.1 licensed — source-available, converts to Apache 2.0 after 3 years

---

## Competitive Landscape

| Competitor | Price | Pros | Cons | O.D.I.N. Advantage |
|-----------|-------|------|------|---------------------|
| **SimplyPrint** | $6-50/mo | Easy setup, cloud dashboard | Cloud-only, data off-network, limited protocols, no air-gap | Self-hosted, 4 protocols, air-gap friendly |
| **3DPrinterOS** | Enterprise $$ | Enterprise features, large fleet support | Cloud-only, per-printer agents, expensive, vendor lock-in | Single container, no agents, transparent pricing |
| **OctoPrint** | Free | Huge plugin ecosystem, mature | Single-printer focused, Pi per printer, no fleet management | Native multi-printer fleet, single install |
| **Mainsail/Fluidd** | Free | Excellent Klipper UI | Klipper only, single printer per instance | Protocol agnostic, unified fleet view |
| **Repetier Server** | $0-40 one-time | Self-hosted, supports USB printers | Dated UI, no AI detection, limited protocol support, no inventory | Modern UI, AI failure detection, full inventory/orders |
| **Manufacturer apps** | Free | Deep integration with own brand | Single brand only, no fleet view, no scheduling | Unified dashboard across all brands |
| **Spreadsheets** | Free | Flexible | No automation, no alerts, doesn't scale | Automated everything |

**Gap:** No self-hosted solution combines multi-protocol fleet management + AI failure detection + job scheduling + inventory + orders + RBAC — in a single Docker container. Cloud solutions (SimplyPrint, 3DPrinterOS) can't serve air-gapped or data-sensitive environments. Open-source tools (OctoPrint, Mainsail) are single-printer focused.

---

## Pricing Model

| Tier | Price | Includes |
|------|-------|----------|
| **Community** | Free forever | Up to 5 printers, 1 user, real-time monitoring, job scheduling, filament inventory, camera feeds, multi-channel alerts, GitHub support |
| **Pro** | $29/mo or $290/yr (save 17%) | Unlimited printers/users, everything in Community + Vigil AI failure detection, orders/products/BOM, analytics, white-label branding, SSO/OIDC, timelapse generation, email support |
| **Education** | $500/yr per campus | Everything in Pro + job approval workflows, per-student quotas, user groups, departmental chargebacks, usage reports, priority support |
| **Enterprise** | Contact sales | Everything in Pro + MFA, IP allowlisting, organizations/multi-tenancy, GDPR export/erasure, compliance docs (FERPA, COPPA, VPAT), dedicated support |

- No credit card for Community — download and run
- 14-day free trial on Pro (no credit card required)
- Ed25519 license files — no phone home, no cloud dependency
- License manager is a separate internal tool (not customer-facing)

**Revenue math:**
- At $29/mo Pro, need ~345 subscribers for $10K MRR
- At $500/yr Education, need 240 campuses for $10K MRR
- 3D printing market growing 20%+ annually; farm management is underserved
- Self-hosted tools have low churn (sticky infrastructure)

---

## Go-to-Market Strategy

### Phase 1: Plant the Flag (Weeks 1-4)

**Goal:** 200 Community installs, 20 Pro trials, establish presence in key communities.

1. **Curl-pipe-bash installer live** — `curl -fsSL https://get.runsodin.com | bash` installs in under 5 minutes. Already built.
2. **Post on r/3Dprinting** — "I built a self-hosted print farm manager that works with Bambu, Klipper, Prusa, and Elegoo from one dashboard." This subreddit (3M+ members) is the exact audience.
3. **Post on r/BambuLab** — Bambu users are the fastest-growing segment and have zero farm management options. "Manage your entire Bambu fleet from one dashboard — plus your Klipper machines too."
4. **Post on r/selfhosted** — position as a single-container self-hosted tool. This community loves Docker-compose-and-done projects.
5. **GitHub README with screenshots** — Dashboard, printer cards, job queue, camera grid, AI detection. Visual proof it works.
6. **runsodin.com landing page** — already built with features, pricing, install guide, and FAQ.

### Phase 2: Build Credibility (Months 2-4)

**Goal:** 500 Community installs, 50 Pro subscribers, first Education pilot.

1. **SEO content:** "How to Manage a 3D Print Farm" (high intent, low competition). "Bambu Lab Farm Setup Guide." "Klipper Multi-Printer Management."
2. **YouTube demo videos:** 5-minute walkthrough showing install → add printers → first print monitored → AI catches failure. The AI detection demo is the money shot.
3. **Bambu Lab community Discord** — answer farm management questions, mention O.D.I.N. where relevant.
4. **Voron/Klipper Discord** — same approach. Klipper users are technical and run fleets.
5. **Education outreach:** Reach out to 10 university fab labs and 10 library makerspaces. Offer first year free in exchange for case study.
6. **Blog posts:** "Why We're Self-Hosted Only" (trust/privacy angle), "Vigil AI: How We Detect Print Failures" (technical credibility)

### Phase 3: Convert and Expand (Months 5-8)

**Goal:** 200 Pro subscribers ($5,800 MRR), 10 Education campuses, first Enterprise leads.

1. **Product Hunt launch** — with demo video showing multi-protocol monitoring + AI detection
2. **3D printing business Facebook groups** — these are where the Etsy/contract printing farm operators live
3. **Conference presence:** MRRF (Midwest RepRap Festival), ERRF (East Coast RepRap Festival) — table or demo station
4. **Education case studies published** — "How [University] Manages 30 Printers with O.D.I.N."
5. **Integration partnerships:** Spoolman (already integrated), OctoPrint (file import), slicer plugins
6. **Enterprise pilots:** Approach 5 manufacturing companies with air-gap/compliance requirements

---

## Distribution Channels (Ranked by ROI)

1. **Reddit (r/3Dprinting, r/BambuLab, r/klippers, r/selfhosted)** — the audience is concentrated, technical, and actively looking for solutions. A single well-received post drives hundreds of installs.
2. **GitHub / open-source discoverability** — "3d print farm manager" searches land on GitHub. Stars and forks compound.
3. **SEO / content marketing** — "bambu farm management," "klipper multi printer," "3d print farm software" are undercontested keywords.
4. **YouTube** — visual demos of the dashboard, AI detection, and multi-printer monitoring sell themselves. Partner with 3D printing YouTubers.
5. **Discord communities** — Bambu Lab, Voron, Klipper servers have thousands of active fleet operators.
6. **Facebook groups** — less technical, but where the small business farm operators are.
7. **Education networks** — ISTE, library innovation labs, STEM education conferences.

---

## Landing Page Copy (Current)

The marketing site (runsodin.com) is already built with:

- **Hero:** "One Dashboard for Every Printer" — feature highlights with animated sections
- **Pain points:** Manual monitoring, brand-specific apps, no unified scheduling
- **How it works:** Install → Connect → Monitor (3 steps)
- **Screenshots:** Dashboard, printer cards, camera grid, job queue, analytics
- **Printer support:** Bambu, Klipper, Prusa, Elegoo logos with protocol details
- **Stats:** Printers monitored, protocols supported, Docker install time
- **Pricing:** Community (free) / Pro ($29/mo) / Education ($500/yr) with feature comparison
- **Testimonials:** Placeholder quotes (need real user testimonials)
- **FAQ:** Install requirements, air-gap support, protocol coverage, data privacy, license model

**CTA hierarchy:**
1. Primary: "Install Now" → docs/install guide
2. Secondary: "Start Free Trial" → Pro trial signup
3. Education: "Contact for Campus Pricing" → email

---

## First 30-Day Marketing Calendar

| Week | Actions |
|------|---------|
| **Week 1** | Polish GitHub README with screenshots + install GIF. Post on r/3Dprinting: "I built a self-hosted print farm manager — Bambu, Klipper, Prusa, Elegoo in one dashboard. Free and open-core." Cross-post r/selfhosted. Monitor feedback, fix any install friction. |
| **Week 2** | Post on r/BambuLab with Bambu-specific angle. Record 5-min YouTube walkthrough. Publish blog: "Why Your Print Farm Needs More Than a Spreadsheet." Start collecting email addresses on landing page. |
| **Week 3** | Hacker News "Show HN" — lead with self-hosted + AI detection + multi-protocol angles. Post in Bambu Lab and Voron Discord servers. Reach out to 3 3D printing YouTubers for review copies (free Pro license). |
| **Week 4** | Publish "Vigil AI: Catching Print Failures Before They Waste Your Filament" blog post with before/after images. Send first outreach emails to 10 university fab labs. Analyze install funnel: download → install success → Pro trial → conversion. |

---

## Customer Acquisition Cost Estimate

- **Organic (Reddit/GitHub/Discord):** $0 — community posts, open-source visibility
- **SEO / content:** $2-5 per trial signup (blog posts, long-tail keywords)
- **YouTube reviews:** $0-100 per video (free Pro license in exchange for review)
- **Conference presence:** $500-1,500 per event (MRRF/ERRF are community events, not trade shows)
- **Paid acquisition:** Not recommended until $10K MRR — organic channels are more effective for this niche
- **Blended target:** <$15 CAC, LTV:CAC > 8:1 (Pro at $290-348/yr, expected 24+ month retention for infrastructure tools)

---

## Key Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Bambu protocol changes** | High | MQTT protocol is reverse-engineered; Bambu could change it. Monitor firmware updates, maintain compat layer. Community will flag changes fast. |
| **SimplyPrint adds self-hosted mode** | Medium | O.D.I.N.'s multi-protocol support and air-gap capability are hard to replicate. First-mover advantage in self-hosted multi-protocol space. |
| **Education sales cycle too long** | Medium | Offer free first year for case study. Schools budget annually — align outreach with fiscal year cycles. |
| **"Why not just use OctoPrint?"** | Medium | Clear positioning: OctoPrint = single printer, O.D.I.N. = fleet. Not competitive, complementary for users who outgrow OctoPrint. |
| **Support burden on free tier** | Low | Community tier gets GitHub Issues only. Pro/Education get email. Keep Community tier genuinely useful but bounded (5 printers, 1 user). |
| **BSL license deters contributors** | Low | Source is available for inspection. Apache 2.0 conversion after 3 years. BSL is increasingly accepted (MariaDB, CockroachDB, Sentry). |

---

## Success Metrics

| Metric | 30 Days | 90 Days | 6 Months |
|--------|---------|---------|----------|
| Community installs | 200 | 750 | 2,500 |
| GitHub stars | 100 | 500 | 1,500 |
| Pro subscribers | 10 | 50 | 200 |
| Education campuses | 0 | 2 | 10 |
| MRR | $290 | $1,450 | $5,800 |
| Discord/community members | 50 | 200 | 500 |
