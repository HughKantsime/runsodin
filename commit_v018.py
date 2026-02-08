#!/usr/bin/env python3
"""
O.D.I.N. v0.18.0 — Full Commit Cycle
1. VERSION bump
2. CHANGELOG update
3. README update (version badge + new features)
4. Print git commands (Hugh runs manually)
"""

import os

BASE = "/opt/printfarm-scheduler"

print("=" * 60)
print("  O.D.I.N. v0.18.0 — Commit Cycle")
print("=" * 60)
print()

# ============================================================
# 1. VERSION
# ============================================================
with open(f"{BASE}/VERSION", "w") as f:
    f.write("0.18.0\n")
print("[1/3] ✅ VERSION → 0.18.0")

# ============================================================
# 2. CHANGELOG
# ============================================================
existing = open(f"{BASE}/CHANGELOG.md").read()

new_entry = """# Changelog

## [0.18.0] - 2026-02-08

### Added — Competitive Feature Parity
- **Frontend license gating** — ProGate component, LicenseContext, Community/Pro tier enforcement on routes, sidebar, and settings
- **MQTT republish** — Forward printer events to external MQTT broker for Home Assistant/Node-RED/Ignition integration
- **Prometheus /metrics endpoint** — Expose printer telemetry in Prometheus format for Grafana dashboards
- **ntfy + Telegram notifications** — Two new notification channels alongside existing Discord/Slack/email/webhooks
- **Quiet hours + daily digest** — Suppress notifications during set hours, aggregate into daily summary email
- **WebSocket real-time updates** — Persistent /ws connection replaces polling for instant printer status updates
- **HMS error decoder** — 42 translated Bambu HMS error codes with human-readable descriptions + fallback for unknown codes
- **Drag-and-drop queue reorder** — Reorder print queue by dragging jobs to new positions
- **PWA manifest** — Progressive Web App support with add-to-homescreen, standalone display, O.D.I.N. branding
- **Keyboard shortcuts** — Global shortcuts (?, /, N, G+D, G+J, G+P, etc.) with help modal
- **3D model viewer** — Three.js interactive preview of .3mf models with orbit controls, wireframe toggle, extracted during upload
- **Smart plug integration** — Tasmota HTTP, Home Assistant REST, and MQTT control with auto power-on/off and configurable cooldown
- **AMS humidity/temperature monitoring** — Time-series capture every 5 minutes per AMS unit with 7-day retention and API
- **Energy consumption tracking** — Per-job kWh and cost tracking via smart plug integration, configurable energy rate
- **i18n multi-language** — React context-based translation system with English (181 keys), German, Japanese, Spanish

### Added — QA & Infrastructure
- **Automated test suite** — 136-check QA script covering database schema, backend files, frontend build, RBAC, license tiers, API endpoints, PWA, service worker
- **RBAC permissions** — Added page access and actions for orders, products, alerts, approval workflow, smart plug control
- **License tier features** — Community tier: keyboard_shortcuts, pwa, i18n, 3d_viewer; Pro tier: smart_plug, energy_tracking, ams_environment, websocket, drag_drop_queue, ntfy, telegram, hms_decoder, failure_logging

### Changed
- I18nProvider wraps app via main.jsx (innermost provider after LicenseProvider)
- Service worker rebranded to O.D.I.N.
- Frontend bundle now ~790KB (up from ~779KB with new features)

### Fixed
- fetchAPI export error in api.js (missing from module exports)
- 3D viewer button JSX nesting in Models.jsx (was inside conditional block)
- SQLAlchemy ForeignKey("users.id") error on job approval columns (no User ORM model)
- I18nProvider imported but not wrapping component tree
- RBAC missing page access entries for products and alerts

"""

# Replace the header + first entry
changelog = existing.replace("# Changelog\n\n## [0.17.0]", new_entry + "## [0.17.0]")
with open(f"{BASE}/CHANGELOG.md", "w") as f:
    f.write(changelog)
print("[2/3] ✅ CHANGELOG.md updated with v0.18.0 entry")

# ============================================================
# 3. README
# ============================================================
readme = open(f"{BASE}/README.md").read()

# Update version badge
readme = readme.replace(
    'img src="https://img.shields.io/badge/version-0.17.0-blue"',
    'img src="https://img.shields.io/badge/version-0.18.0-blue"'
)

# Update Features section — add new sections after Notifications
old_notifications = """### Notifications
- **Browser push** — VAPID-based notifications via service worker
- **Webhooks** — Discord and Slack integration with alert type filtering
- **Email** — SMTP-based alerts for print complete, failures, maintenance due
- **In-app alerts** — bell icon with unread count, filterable alerts page"""

new_notifications = """### Notifications
- **Browser push** — VAPID-based notifications via service worker
- **Webhooks** — Discord and Slack integration with alert type filtering
- **Email** — SMTP-based alerts for print complete, failures, maintenance due
- **In-app alerts** — bell icon with unread count, filterable alerts page
- **ntfy + Telegram** — lightweight push via ntfy.sh or Telegram Bot API
- **Quiet hours** — suppress notifications overnight, get a daily digest instead

### Integrations & Monitoring
- **MQTT republish** — forward printer events to external broker for Home Assistant, Node-RED, Ignition
- **Prometheus /metrics** — expose telemetry for Grafana dashboards
- **Smart plug control** — Tasmota, Home Assistant, or MQTT-based power management with auto on/off
- **Energy tracking** — per-job electricity cost via smart plug kWh monitoring
- **AMS environment** — humidity and temperature monitoring with 7-day history
- **WebSocket** — real-time push updates (no more polling)

### 3D & UX
- **3D model viewer** — interactive Three.js preview of .3mf files with orbit controls
- **Drag-and-drop queue** — reorder print jobs by dragging
- **Keyboard shortcuts** — global hotkeys with ? help modal
- **PWA support** — install as a native app on mobile and desktop
- **Multi-language** — English, Deutsch, 日本語, Español (community contributions welcome)"""

readme = readme.replace(old_notifications, new_notifications)

# Update tier table — add MQTT Republish to Pro
old_tier_row = '| **OPC-UA / MQTT Republish** | — | — | — | ✅ |'
new_tier_row = '| **MQTT Republish** | — | ✅ | ✅ | ✅ |\n| **OPC-UA** | — | — | — | ✅ |'
readme = readme.replace(old_tier_row, new_tier_row)

# Add Prometheus and Smart Plug to tier table
old_audit = '| **Audit Export** | — | — | — | ✅ |'
new_audit = '| **Prometheus Metrics** | — | ✅ | ✅ | ✅ |\n| **Smart Plug Control** | — | ✅ | ✅ | ✅ |\n| **Audit Export** | — | — | — | ✅ |'
readme = readme.replace(old_audit, new_audit)

with open(f"{BASE}/README.md", "w") as f:
    f.write(readme)
print("[3/3] ✅ README.md updated (version badge, features, tier table)")

# ============================================================
# DONE — print git commands
# ============================================================
print()
print("=" * 60)
print("  Files updated. Run these commands to commit:")
print("=" * 60)
print()
print("cd /opt/printfarm-scheduler")
print()
print("# Rebuild frontend (version badge updated)")
print("cd frontend && npm run build && cd ..")
print()
print("# Stage everything")
print("git add -A")
print()
print("# Commit")
print('git commit -m "v0.18.0 — Competitive feature parity: license gating, MQTT republish, Prometheus, WebSocket, 3D viewer, smart plugs, AMS monitoring, i18n, quiet hours, drag-drop queue, PWA, keyboard shortcuts, ntfy/Telegram, energy tracking, HMS decoder, automated QA suite"')
print()
print("# Tag")
print("git tag -a v0.18.0 -m 'v0.18.0 — Competitive feature parity + Bambuddy gap closure'")
print()
print("# Push")
print("git push origin main --tags")
print()
print("=" * 60)
