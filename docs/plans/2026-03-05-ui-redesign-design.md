# O.D.I.N. UI Redesign — Design Document

**Date:** 2026-03-05
**Status:** Approved
**Scope:** Visual-only redesign of the entire frontend. No functionality changes.

## Design Philosophy

"Precision engineering, not decoration."

Every visual element earns its place by conveying information. Inspired by Orca Slicer's technical clarity, Apple's restraint, and Google Cloud's information density. The UI should feel like a well-designed instrument panel — dense with data but immediately scannable.

References: Bambu Lab Orca Slicer, Apple, Microsoft, Google Cloud Console.

## Decisions

| Decision | Choice | Reasoning |
|----------|--------|-----------|
| Color direction | Warm industrial, refined | Keep amber/bronze but desaturate and restrict usage |
| Layout density | Information-dense but organized | All data visible, better hierarchy/whitespace/grouping |
| Sidebar style | Keep grouped structure, refined | Clean up visually, maintain current organization |
| Typography | Keep IBM Plex pairing, refined | Good fonts, refine sizing hierarchy and application |
| Light/dark mode | Both modes, equal priority | Both need to look equally polished |
| Filament visualization | Spool ring indicators | Circular arc showing color + material + fill level |

---

## 1. Color System

Warm industrial, refined. Keep amber/bronze but desaturate and restrict usage.

| Role | Dark Mode | Light Mode |
|------|-----------|------------|
| Background | `#0B0D11` | `#F4F5F7` |
| Surface/Card | `#12151B` | `#FFFFFF` |
| Elevated surface | `#1A1D25` | `#FFFFFF` + shadow |
| Border | `#1F2330` | `#E2E5EB` |
| Text primary | `#E8ECF2` | `#0F1219` |
| Text secondary | `#7A8396` | `#5A6478` |
| Text muted | `#3D4559` | `#9BA3B2` |
| Accent primary | `#C47A1A` | `#9A5E0D` |
| Accent hover | `#D4891F` | `#B46E14` |

Status colors (slightly muted in dark mode):
- Printing: `#5B93E8`
- Completed: `#3DAF5C`
- Failed: `#D84848`
- Warning: `#D4891F`

Rule: Accent color appears only on active nav items, primary buttons, focus rings, and selected states.

## 2. Typography Hierarchy

| Level | Font | Weight | Size | Tracking | Usage |
|-------|------|--------|------|----------|-------|
| Page title | Plex Mono | 600 | 20px | 0.01em | Page headers only |
| Section heading | Plex Sans | 600 | 14px | 0 | Card titles, group labels |
| Body | Plex Sans | 400 | 13px | 0 | General text |
| Caption | Plex Sans | 500 | 11px | 0.02em | Labels, metadata |
| Data value | Plex Mono | 500 | 13px | 0 | Temperatures, percentages, counts |
| Nav group label | Plex Mono | 500 | 10px | 0.15em | Sidebar section headers |

Key changes: Reduce letter-spacing on headings. Use mono only for page titles and data values. Tighter size scale.

## 3. Sidebar Navigation

- Active state: Left border accent bar + subtle background tint (no saturated highlight)
- Section dividers: Thin 1px line. Group labels stay uppercase mono at lower contrast
- Fleet status: Compact horizontal bar chart replacing colored dots
- Spacing: Tighter vertical rhythm (py-1.5), items feel more like a list
- Collapsed mode: Clean icon rail with 1px accent bar for active item

## 4. Cards & Surfaces

- Cards use background differentiation only, no visible borders in dark mode
- Light mode: subtle box-shadow instead of borders
- Consistent 16px padding
- Card headers: left-aligned text only, no icon badges
- Hover: subtle background brightness change (no translateY)

## 5. Printer Cards

```
+-------------------------------------------+
| X1C                              . Online  |
| Bambu Lab X1 Carbon                        |
|                                            |
| (o) PLA Matte  (o) PETG HF  (o) PLA Galaxy|  <- spool rings
| (o) PLA Basic  ( ) Empty     ( ) Empty     |  <- empty = dashed
|                                            |
| Nozzle 210   Bed 60            ####_ 73%   |  <- progress if printing
|                                            |
| AMS . Telemetry . Nozzle . HMS             |  <- text tabs, no pills
+-------------------------------------------+
```

### Spool Ring Indicators

- 20px diameter circular arc (SVG)
- Stroke = filament color, stroke-width = 3px
- Arc fill = remaining percentage (full circle = 100%)
- Below 15%: dashed stroke + amber warning border
- Material label right of ring in caption text
- Empty slots: gray dashed circle + "Empty" label

## 6. Buttons & Controls

- Primary: Solid accent fill, white text. Used sparingly (1 per view).
- Secondary: Border-only (1px border, transparent fill).
- Ghost: Text + icon only, no background.
- Danger: Red border-only by default, solid red fill only in destructive confirmations.
- Drop success, warning, tertiary variants.
- Border radius: 6px
- All icon buttons: 32px touch target

## 7. Status Indicators

- Status dot: 6px, solid color, no glow
- Printing: subtle pulse animation (no glow shadow)
- Inline status: plain text in status color, no background badge
- Badge variant (tables/lists): subtle tinted background (status-color/8%), no border

## 8. Tables

- Keep striped pattern, remove hover background shift
- Hover: subtle left-border accent
- Header: sticky, heavier bottom border, sentence case + font-weight 600 (no uppercase)
- Cell padding: 12px vertical, 16px horizontal
- Sortable columns: small chevron indicator

## 9. Notification Alert Types

Replace all emoji with Lucide icons:

| Alert Type | Icon |
|------------|------|
| Print Complete | CheckCircle (green) |
| Print Failed | XCircle (red) |
| Spool Low | AlertTriangle (amber) |
| Maintenance Due | Wrench (muted) |
| Job Submitted | FileText (muted) |
| Job Approved | CheckCircle (blue) |
| Job Rejected | XOctagon (red) |
| Bed Cooled | Thermometer (blue) |
| Job Queued | ListPlus (muted) |
| Job Skipped | SkipForward (muted) |
| Failed to Start | AlertTriangle (red) |

## 10. Modals & Drawers

- Backdrop: black/60%
- Modal surface: card surface color, no extra border
- Title: Plex Sans 600, 14px (not monospace)
- Close: top-right ghost X
- Actions: right-aligned, primary + secondary only

## 11. Charts (Recharts)

- Grid lines: `#1A1D25` (barely visible)
- Axis text: 11px, muted color
- Tooltips: card surface, subtle shadow, no border
- Chart colors: restrained 5-color palette from status colors
- Solid colors at low opacity for area charts (no gradients)

## 12. Light Mode Strategy

- Every themed color must be a CSS variable
- Reduce 300+ lines of light mode overrides via better variable coverage
- Status colors identical in both modes
- Light mode gets slightly warmer grays to match amber accent

## 13. What Does NOT Change

- All functionality, routes, API calls, data flow
- React Query / WebSocket architecture
- Page structure and component hierarchy
- Sidebar navigation items and groupings
- Mobile responsive behavior
- Accessibility features (focus rings, ARIA, keyboard nav)
- Branding system (custom fonts/colors/logo)

## 14. Camera Views

### 14a. Camera Grid Page (`/cameras`)

- Cards: borderless dark surface, 6px radius. Video fills card edges
- Status overlay: Bottom-left, 6px green dot + "LIVE" mono text. No pulse animation
- Controls overlay: Top-right on hover, frosted glass (backdrop-blur-sm bg-black/40)
- Camera name + AI badge: Slim 28px bar below video
- Grid toggle: Segmented control (1x1 | 2x2 | 3x3), border-only segments
- Filter input: Bottom-border only (no background box)

### 14b. Camera Detail Page (`/cameras/:id`)

- 70/30 split maintained. Video container: zero radius, full bleed, 1px bottom border
- Breadcrumb: arrow ghost button + slash divider + printer name in heading weight
- Info panels: Borderless sections with thin dividers. Titles in 11px uppercase muted caption
- Snapshot button: Hover-reveal, frosted glass, top-right

### 14c. Control Room Mode

- Header: 40px slim toolbar. "Control Room" in section heading weight. Mono clock + "Exit" text button
- Camera tiles: Zero gap, 1px dark border separation. Camera names as 10px mono overlays
- No rounded corners on any tile
- Connection status: Small colored dot top-right (no text). Spinner for reconnecting

### 14d. Picture-in-Picture (PiP)

- 6px radius, 1px border, subtle shadow
- 24px header: solid bg-farm-900/90 + backdrop-blur. Name left, buttons right
- LIVE dot bottom-left (consistent)
- 4-dot grip icon on hover (replacing Move icon)

### 14e. Camera Modal

- Small PIP: Same as 14d
- Large modal: Video fills modal content. Status bar below (name left, status right). Info bar only when printing
- Fullscreen: Video full viewport. Side panel (300px) slides in with 200ms ease, toggleable

### Camera Consistency Rules

| Element | Standard |
|---------|----------|
| LIVE indicator | 6px green dot + "LIVE" 10px Plex Mono, white/70% |
| Offline indicator | 6px red dot + "OFFLINE" 10px Plex Mono |
| Connecting state | 6px amber dot (no pulse) + "CONNECTING" |
| Error state | VideoOff icon 24px muted + "Reconnect" ghost button |
| Video controls | Frosted glass, 6px radius, hover reveal |
| Camera name | 13px Plex Sans 500 |
| Temperature display | Mono font, degree symbol, no icon |
