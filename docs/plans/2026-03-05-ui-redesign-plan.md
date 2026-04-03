# O.D.I.N. UI Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the entire O.D.I.N. frontend to a clean, professional, industrial aesthetic — inspired by Orca Slicer, Apple, and Google Cloud — without changing any functionality.

**Architecture:** Pure visual refactor. Changes cascade from design tokens (CSS variables, Tailwind config) through UI primitives (Button, Card, Modal) into layout (Sidebar, App shell) and finally into all 36 page components. No backend changes, no new routes, no API changes. Existing ~1966 tests must continue passing.

**Tech Stack:** React 18, Tailwind CSS 3, IBM Plex fonts, Lucide React icons, Recharts, CSS custom properties.

**Design Doc:** `docs/plans/2026-03-05-ui-redesign-design.md`

---

## Wave 1: Design Foundation

Everything else depends on these tokens. Get them right first.

### Task 1: Update CSS Variables and Root Styles

**Files:**
- Modify: `frontend/src/index.css`

**Context:** The root CSS file defines all CSS custom properties (brand colors, farm palette, chart theming, typography), base styles, component utilities (table-industrial, scrollbar, status-dot), and light mode overrides. This is the single most important file — every component reads from these variables.

**Step 1: Update dark mode CSS variables (`:root` block, lines 6-53)**

Replace the current values with the refined palette:

```css
:root {
  /* Brand colors — Refined warm industrial */
  --brand-primary: #C47A1A;
  --brand-accent: #D4891F;
  --brand-sidebar-bg: #0B0D11;
  --brand-sidebar-border: #1F2330;
  --brand-sidebar-text: #7A8396;
  --brand-sidebar-active-bg: rgba(196, 122, 26, 0.08);
  --brand-sidebar-active-text: #D4891F;
  --brand-content-bg: #0B0D11;
  --brand-card-bg: #12151B;
  --brand-card-border: #1F2330;
  --brand-text-primary: #E8ECF2;
  --brand-text-secondary: #7A8396;
  --brand-text-muted: #3D4559;
  --brand-input-bg: #1A1D25;
  --brand-input-border: #1F2330;
  --brand-input-text: #E8ECF2;
  /* Chart theming */
  --chart-card-bg: #12151B;
  --chart-grid: #1A1D25;
  --chart-axis: #3D4559;
  --chart-axis-line: #1F2330;
  --chart-tooltip-bg: #12151B;
  --chart-tooltip-border: #1F2330;
  --chart-tooltip-shadow: 0 10px 25px rgba(0,0,0,0.5);
  /* Brand fonts — IBM Plex (unchanged) */
  --brand-font-display: 'IBM Plex Mono', ui-monospace, monospace;
  --brand-font-body: 'IBM Plex Sans', system-ui, sans-serif;
  --brand-font-mono: 'IBM Plex Mono', ui-monospace, monospace;
  /* UI chrome */
  --brand-focus-ring: #C47A1A;
  --brand-selection-bg: rgba(196, 122, 26, 0.25);
  --brand-selection-text: #D4891F;
  --brand-card-shadow: none;
  --brand-job-card-hover-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  /* Farm palette — refined dark slate */
  --farm-50: 232 236 242;
  --farm-100: 212 218 228;
  --farm-200: 184 194 208;
  --farm-300: 139 149 168;
  --farm-400: 122 131 150;
  --farm-500: 74 85 104;
  --farm-600: 61 69 89;
  --farm-700: 26 29 37;
  --farm-800: 18 21 27;
  --farm-900: 11 13 17;
  --farm-950: 11 13 17;
  /* Status colors — muted for dark mode */
  --status-printing: #5B93E8;
  --status-completed: #3DAF5C;
  --status-failed: #D84848;
  --status-warning: #D4891F;
  --status-pending: #7A8396;
  --status-scheduled: #8B7BE8;
}
```

**Step 2: Update base styles (lines 60-90)**

Refine typography — tighten letter-spacing, use mono only for page titles and data:

```css
@layer base {
  body {
    @apply antialiased;
    font-family: var(--brand-font-body);
    background: var(--brand-content-bg);
    color: rgb(var(--farm-300));
    font-size: 13px;
    line-height: 1.5;
  }

  h1 {
    font-family: var(--brand-font-display);
    letter-spacing: 0.01em;
    font-weight: 600;
    font-size: 20px;
  }

  h2, h3, h4, h5, h6 {
    font-family: var(--brand-font-body);
    font-weight: 600;
    font-size: 14px;
    letter-spacing: 0;
  }

  code, pre, .mono {
    font-family: var(--brand-font-mono);
  }

  input, select, textarea {
    border-radius: 6px !important;
    background-color: var(--brand-input-bg);
    border-color: var(--brand-input-border);
    color: var(--brand-input-text);
  }

  button {
    border-radius: 6px;
  }
}
```

**Step 3: Update component utilities**

Replace `table-industrial` hover with left-border accent:

```css
@layer components {
  .table-industrial tbody tr:nth-child(even) {
    background-color: rgba(var(--farm-800) / 0.4);
  }
  .table-industrial tbody tr {
    @apply border-b;
    border-color: var(--brand-card-border);
    transition: border-color 0.1s ease;
  }
  .table-industrial tbody tr:hover {
    border-left: 2px solid var(--brand-primary);
  }
  .table-industrial thead {
    background: var(--brand-card-bg);
    @apply border-b;
    border-color: var(--brand-card-border);
    position: sticky;
    top: 0;
    z-index: 1;
  }
  .table-industrial th {
    @apply text-xs font-semibold tracking-normal;
    color: var(--brand-text-secondary);
    padding: 12px 16px;
    text-transform: none;
  }
  .table-industrial td {
    padding: 12px 16px;
  }
}
```

Remove the glow from status dots:

```css
.status-dot {
  @apply w-1.5 h-1.5 rounded-full;
}
.status-dot.printing {
  background: var(--status-printing);
  animation: statusPulse 2s ease-in-out infinite;
}
@keyframes statusPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
```

Remove job card translateY hover — replace with brightness:

```css
.job-card {
  transition: background-color 0.15s ease;
}
.job-card:hover {
  filter: brightness(1.15);
}
```

**Step 4: Update light mode variables (html.light block)**

```css
html.light {
  --brand-primary: #9A5E0D;
  --brand-accent: #B46E14;
  --brand-sidebar-bg: #F8FAFC;
  --brand-sidebar-border: #E2E5EB;
  --brand-sidebar-text: #1E293B;
  --brand-sidebar-active-bg: rgba(154, 94, 13, 0.06);
  --brand-sidebar-active-text: #9A5E0D;
  --brand-content-bg: #F4F5F7;
  --brand-card-bg: #FFFFFF;
  --brand-card-border: #E2E5EB;
  --brand-text-primary: #0F1219;
  --brand-text-secondary: #5A6478;
  --brand-text-muted: #9BA3B2;
  --brand-input-bg: #F8FAFC;
  --brand-input-border: #E2E5EB;
  --brand-input-text: #1E293B;
  --chart-card-bg: #FFFFFF;
  --chart-grid: #E2E5EB;
  --chart-axis: #5A6478;
  --chart-axis-line: #E2E5EB;
  --chart-tooltip-bg: #FFFFFF;
  --chart-tooltip-border: #E2E5EB;
  --chart-tooltip-shadow: 0 4px 12px rgba(0,0,0,0.08);
  --brand-font-display: inherit;
  --brand-font-body: inherit;
  --brand-font-mono: inherit;
  --brand-focus-ring: #9A5E0D;
  --brand-selection-bg: rgba(154, 94, 13, 0.15);
  --brand-selection-text: #9A5E0D;
  --brand-card-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02);
  --brand-job-card-hover-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  /* Farm palette — light mode */
  --farm-50: 2 6 23;
  --farm-100: 15 23 42;
  --farm-200: 30 41 59;
  --farm-300: 51 65 85;
  --farm-400: 71 85 105;
  --farm-500: 75 85 99;
  --farm-600: 148 163 184;
  --farm-700: 226 229 235;
  --farm-800: 226 232 240;
  --farm-900: 255 255 255;
  --farm-950: 244 245 247;
  /* Status colors same in both modes */
  --status-printing: #3B82F6;
  --status-completed: #22C55E;
  --status-failed: #EF4444;
  --status-warning: #D97706;
  --status-pending: #6B7280;
  --status-scheduled: #8B5CF6;
}
```

Keep existing light mode semantic overrides (html.light .bg-red-900/30, etc.) but clean up the text-white remapping section — keep it but ensure it's using the updated farm variables.

**Step 5: Verify no visual regressions**

Run: `cd /workspace/odin/frontend && npm run build`
Expected: Build succeeds with no errors.

**Step 6: Commit**

```
style(design-system): update CSS variables for refined warm industrial palette

- Desaturated amber accent (#C47A1A vs #d97706)
- Refined dark slate backgrounds
- Added status color CSS variables
- Updated light mode palette
- Removed status dot glow effect
- Replaced card hover translate with brightness filter
- Table headers: sentence case, sticky, consistent padding
```

---

### Task 2: Update Tailwind Config

**Files:**
- Modify: `frontend/tailwind.config.js`

**Step 1: Update the Tailwind config to match new design tokens**

```javascript
/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'farm': {
          50:  'rgb(var(--farm-50) / <alpha-value>)',
          100: 'rgb(var(--farm-100) / <alpha-value>)',
          200: 'rgb(var(--farm-200) / <alpha-value>)',
          300: 'rgb(var(--farm-300) / <alpha-value>)',
          400: 'rgb(var(--farm-400) / <alpha-value>)',
          500: 'rgb(var(--farm-500) / <alpha-value>)',
          600: 'rgb(var(--farm-600) / <alpha-value>)',
          700: 'rgb(var(--farm-700) / <alpha-value>)',
          800: 'rgb(var(--farm-800) / <alpha-value>)',
          900: 'rgb(var(--farm-900) / <alpha-value>)',
          950: 'rgb(var(--farm-950) / <alpha-value>)',
        },
        'print': {
          50:  '#FFF9EB',
          100: '#FFF0CC',
          200: '#FFDFA3',
          300: '#D4891F',
          400: '#C47A1A',
          500: 'var(--brand-primary, #C47A1A)',
          600: 'var(--brand-primary, #9A5E0D)',
          700: '#7A4B0A',
          800: '#5A3707',
          900: '#3A2404',
        },
        'status': {
          pending:   'var(--status-pending, #7A8396)',
          scheduled: 'var(--status-scheduled, #8B7BE8)',
          printing:  'var(--status-printing, #5B93E8)',
          completed: 'var(--status-completed, #3DAF5C)',
          failed:    'var(--status-failed, #D84848)',
        }
      },
      fontFamily: {
        'mono':    ['var(--brand-font-mono)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'display': ['var(--brand-font-display)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'body':    ['var(--brand-font-body)', '"IBM Plex Sans"', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        'DEFAULT': '6px',
        'sm': '4px',
        'md': '6px',
        'lg': '8px',
        'xl': '10px',
        '2xl': '12px',
      },
      fontSize: {
        'xs': ['11px', { lineHeight: '1.5' }],
        'sm': ['13px', { lineHeight: '1.5' }],
        'base': ['14px', { lineHeight: '1.6' }],
        'lg': ['16px', { lineHeight: '1.5' }],
        'xl': ['20px', { lineHeight: '1.3' }],
      },
    },
  },
  plugins: [],
}
```

Key changes from current:
- `borderRadius.DEFAULT`: 4px -> 6px
- `print` color scale: desaturated amber values
- `status` colors: now use CSS variables for theme-awareness
- Added `fontSize` scale to enforce consistent typography
- Print-500/600 now reference `--brand-primary` (was `#d97706`/`#b45309`)

**Step 2: Build to verify**

Run: `cd /workspace/odin/frontend && npm run build`
Expected: Build succeeds.

**Step 3: Commit**

```
style(tailwind): update config for refined design system

- Border radius: 4px -> 6px default
- Desaturated print accent scale
- Status colors via CSS variables
- Added fontSize scale (xs:11px, sm:13px, base:14px)
```

---

## Wave 2: UI Primitives

These are used everywhere. Update them and most pages improve automatically.

### Task 3: Redesign Button Component

**Files:**
- Modify: `frontend/src/components/ui/Button.jsx`

**Context:** Current button has 7 variants (primary, secondary, tertiary, danger, success, warning, ghost). Design calls for reducing to 4 (primary, secondary, ghost, danger) and updating colors.

**Step 1: Read the current file**

Read: `frontend/src/components/ui/Button.jsx`

**Step 2: Update variant classes**

Replace the `variantClasses` object:

```javascript
const variantClasses = {
  primary: 'bg-[var(--brand-primary)] hover:bg-[var(--brand-accent)] text-white',
  secondary: 'border border-[var(--brand-card-border)] bg-transparent hover:bg-farm-800 text-[var(--brand-text-secondary)]',
  ghost: 'text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] hover:bg-farm-800',
  danger: 'border border-red-500/30 text-red-400 hover:bg-red-500/10',
}
```

Update `sizeClasses`:

```javascript
const sizeClasses = {
  sm: 'px-2.5 py-1 text-xs gap-1.5',
  md: 'px-3.5 py-1.5 text-sm gap-2',
  lg: 'px-5 py-2.5 text-sm gap-2',
  icon: 'p-1.5 min-w-[32px] min-h-[32px] flex items-center justify-center',
}
```

Remove `rounded-md` and `rounded-lg` from size classes (border-radius now comes from Tailwind default at 6px). Add a base class instead:

```javascript
const baseClasses = 'inline-flex items-center justify-center font-medium transition-colors rounded-md disabled:opacity-50 disabled:cursor-not-allowed'
```

**Step 3: Handle backward compatibility for removed variants**

Map old variants to new ones so nothing breaks:

```javascript
const variantMap = {
  primary: 'primary',
  secondary: 'secondary',
  tertiary: 'secondary',   // tertiary -> secondary
  danger: 'danger',
  success: 'primary',      // success -> primary
  warning: 'primary',      // warning -> primary
  ghost: 'ghost',
}
```

Use this at the top of the component: `const resolvedVariant = variantMap[variant] || 'primary'`

**Step 4: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`
Expected: Build succeeds. No broken imports since the component still accepts all old variant strings.

**Step 5: Commit**

```
style(ui): redesign Button component

- Reduced to 4 variants (primary, secondary, ghost, danger)
- Backward-compatible mapping for removed variants
- 32px minimum touch target for icon buttons
- Colors use CSS variables for theme awareness
```

---

### Task 4: Redesign Card Component

**Files:**
- Modify: `frontend/src/components/ui/Card.jsx`

**Context:** Design calls for: no visible border in dark mode (use background differentiation), light mode gets box-shadow, consistent 16px padding, no hover translate.

**Step 1: Read the current file**

Read: `frontend/src/components/ui/Card.jsx`

**Step 2: Rewrite the Card component**

```jsx
import clsx from 'clsx'

export default function Card({ children, className, padding = 'md', selected, onClick, ...props }) {
  const paddingClasses = {
    sm: 'p-3',
    md: 'p-4',
    lg: 'p-5',
  }

  return (
    <div
      className={clsx(
        'rounded-md transition-colors',
        'bg-[var(--brand-card-bg)]',
        'shadow-[var(--brand-card-shadow)]',
        selected && 'ring-1 ring-[var(--brand-primary)]',
        onClick && 'cursor-pointer hover:brightness-110',
        paddingClasses[padding],
        className
      )}
      onClick={onClick}
      {...props}
    >
      {children}
    </div>
  )
}
```

Key changes:
- No `border-farm-800` in dark mode — uses background differentiation
- `box-shadow` via CSS variable (none in dark, subtle in light)
- Selected state uses ring instead of border color change
- Hover uses brightness instead of background change
- Consistent padding (sm=12, md=16, lg=20)

**Step 3: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 4: Commit**

```
style(ui): redesign Card component

- No visible border in dark mode (background differentiation)
- Light mode uses box-shadow
- Selected state via ring
- Consistent 16px default padding
```

---

### Task 5: Redesign StatusBadge Component

**Files:**
- Modify: `frontend/src/components/ui/StatusBadge.jsx`

**Context:** Design calls for: 6px dot (no glow), inline text in status color (no background badge), badge variant with subtle tinted bg (8% opacity).

**Step 1: Read the current file**

Read: `frontend/src/components/ui/StatusBadge.jsx`

**Step 2: Rewrite with two modes**

```jsx
import clsx from 'clsx'

const statusColors = {
  pending:    { dot: 'bg-[var(--status-pending)]',    text: 'text-[var(--status-pending)]',    bg: 'bg-[var(--status-pending)]/8' },
  queued:     { dot: 'bg-[var(--status-pending)]',    text: 'text-[var(--status-pending)]',    bg: 'bg-[var(--status-pending)]/8' },
  scheduled:  { dot: 'bg-[var(--status-scheduled)]',  text: 'text-[var(--status-scheduled)]',  bg: 'bg-[var(--status-scheduled)]/8' },
  printing:   { dot: 'bg-[var(--status-printing)]',   text: 'text-[var(--status-printing)]',   bg: 'bg-[var(--status-printing)]/8' },
  running:    { dot: 'bg-[var(--status-printing)]',   text: 'text-[var(--status-printing)]',   bg: 'bg-[var(--status-printing)]/8' },
  paused:     { dot: 'bg-[var(--status-warning)]',    text: 'text-[var(--status-warning)]',    bg: 'bg-[var(--status-warning)]/8' },
  completed:  { dot: 'bg-[var(--status-completed)]',  text: 'text-[var(--status-completed)]',  bg: 'bg-[var(--status-completed)]/8' },
  done:       { dot: 'bg-[var(--status-completed)]',  text: 'text-[var(--status-completed)]',  bg: 'bg-[var(--status-completed)]/8' },
  failed:     { dot: 'bg-[var(--status-failed)]',     text: 'text-[var(--status-failed)]',     bg: 'bg-[var(--status-failed)]/8' },
  error:      { dot: 'bg-[var(--status-failed)]',     text: 'text-[var(--status-failed)]',     bg: 'bg-[var(--status-failed)]/8' },
  cancelled:  { dot: 'bg-[var(--status-pending)]',    text: 'text-[var(--status-pending)]',    bg: 'bg-[var(--status-pending)]/8' },
  rejected:   { dot: 'bg-[var(--status-failed)]',     text: 'text-[var(--status-failed)]',     bg: 'bg-[var(--status-failed)]/8' },
  approved:   { dot: 'bg-[var(--status-completed)]',  text: 'text-[var(--status-completed)]',  bg: 'bg-[var(--status-completed)]/8' },
}

const defaultColor = { dot: 'bg-[var(--status-pending)]', text: 'text-[var(--status-pending)]', bg: 'bg-[var(--status-pending)]/8' }

function normalizeStatus(status) {
  return (status || 'pending').toLowerCase().replace(/[-_\s]/g, '')
}

export default function StatusBadge({ status, size = 'sm', variant = 'inline' }) {
  const key = normalizeStatus(status)
  const color = statusColors[key] || defaultColor
  const label = (status || 'pending').replace(/[-_]/g, ' ')
  const isPrinting = key === 'printing' || key === 'running'

  if (variant === 'badge') {
    return (
      <span className={clsx(
        'inline-flex items-center gap-1.5 rounded-sm px-2 py-0.5',
        color.bg,
        color.text,
        size === 'sm' ? 'text-xs' : 'text-sm'
      )}>
        <span className={clsx('w-1.5 h-1.5 rounded-full', color.dot, isPrinting && 'animate-pulse')} />
        <span className="capitalize font-medium">{label}</span>
      </span>
    )
  }

  // inline variant — just dot + text, no background
  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5',
      color.text,
      size === 'sm' ? 'text-xs' : 'text-sm'
    )}>
      <span className={clsx('w-1.5 h-1.5 rounded-full', color.dot, isPrinting && 'animate-pulse')} />
      <span className="capitalize font-medium">{label}</span>
    </span>
  )
}
```

**Step 3: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 4: Commit**

```
style(ui): redesign StatusBadge with inline/badge variants

- 6px dots, no glow effect
- Inline variant: text only in status color
- Badge variant: subtle 8% tinted background
- Status colors via CSS variables for theme awareness
```

---

### Task 6: Redesign StatCard, PageHeader, EmptyState, TabBar

**Files:**
- Modify: `frontend/src/components/ui/StatCard.jsx`
- Modify: `frontend/src/components/ui/PageHeader.jsx`
- Modify: `frontend/src/components/ui/EmptyState.jsx`
- Modify: `frontend/src/components/ui/TabBar.jsx`

**Step 1: Read all four files**

**Step 2: Update StatCard**

Remove colored background variants. Use card surface with subtle left-border accent:

```jsx
import clsx from 'clsx'

const accentColors = {
  green: 'border-l-[var(--status-completed)]',
  blue: 'border-l-[var(--status-printing)]',
  amber: 'border-l-[var(--brand-primary)]',
  red: 'border-l-[var(--status-failed)]',
  purple: 'border-l-[var(--status-scheduled)]',
  default: 'border-l-transparent',
}

export default function StatCard({ icon: Icon, label, value, subtitle, color = 'default', onClick, className }) {
  return (
    <div
      className={clsx(
        'bg-[var(--brand-card-bg)] shadow-[var(--brand-card-shadow)] rounded-md p-4 border-l-2 transition-colors',
        accentColors[color] || accentColors.default,
        onClick && 'cursor-pointer hover:brightness-110',
        className
      )}
      onClick={onClick}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-[var(--brand-text-muted)] uppercase tracking-wider">{label}</span>
        {Icon && <Icon size={16} className="text-[var(--brand-text-muted)]" />}
      </div>
      <div className="font-mono font-semibold text-xl text-[var(--brand-text-primary)]">{value}</div>
      {subtitle && <div className="text-xs text-[var(--brand-text-secondary)] mt-1">{subtitle}</div>}
    </div>
  )
}
```

**Step 3: Update PageHeader**

Refine typography — h1 page title in Plex Mono 20px, subtitle in secondary:

```jsx
export default function PageHeader({ icon: Icon, title, subtitle, children }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6">
      <div className="flex items-center gap-3">
        {Icon && <Icon size={20} className="text-[var(--brand-primary)] flex-shrink-0" />}
        <div>
          <h1 className="font-display font-semibold text-xl text-[var(--brand-text-primary)] tracking-tight">{title}</h1>
          {subtitle && <p className="text-xs text-[var(--brand-text-secondary)] mt-0.5">{subtitle}</p>}
        </div>
      </div>
      {children && <div className="flex items-center gap-2 flex-wrap">{children}</div>}
    </div>
  )
}
```

**Step 4: Update EmptyState**

Cleaner, muted:

```jsx
export default function EmptyState({ icon: Icon, title, description, children }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {Icon && <Icon size={32} className="text-[var(--brand-text-muted)] mb-3" />}
      <h3 className="font-semibold text-sm text-[var(--brand-text-primary)] mb-1">{title}</h3>
      {description && <p className="text-xs text-[var(--brand-text-secondary)] max-w-sm">{description}</p>}
      {children && <div className="mt-4">{children}</div>}
    </div>
  )
}
```

**Step 5: Update TabBar**

Replace pill with underline-style active indicator:

```jsx
import clsx from 'clsx'

export default function TabBar({ tabs, activeTab, onTabChange, variant = 'default' }) {
  return (
    <div className={clsx(
      'flex gap-1',
      variant === 'inline' ? 'border-b border-[var(--brand-card-border)]' : 'bg-[var(--brand-card-bg)] rounded-md p-1'
    )}>
      {tabs.map(tab => {
        const key = tab.key || tab.label || tab
        const label = tab.label || tab
        const Icon = tab.icon
        const count = tab.count
        const isActive = activeTab === key

        return (
          <button
            key={key}
            onClick={() => onTabChange(key)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors rounded-sm',
              variant === 'inline'
                ? isActive
                  ? 'text-[var(--brand-primary)] border-b-2 border-[var(--brand-primary)] -mb-px'
                  : 'text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)]'
                : isActive
                  ? 'bg-[var(--brand-primary)] text-white'
                  : 'text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] hover:bg-farm-800'
            )}
          >
            {Icon && <Icon size={14} />}
            {label}
            {count != null && (
              <span className={clsx(
                'text-[10px] px-1.5 py-0.5 rounded-sm font-mono',
                isActive ? 'bg-white/20' : 'bg-farm-800 text-[var(--brand-text-muted)]'
              )}>{count}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}
```

**Step 6: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 7: Commit**

```
style(ui): redesign StatCard, PageHeader, EmptyState, TabBar

- StatCard: left-border accent, no colored backgrounds
- PageHeader: refined typography hierarchy
- EmptyState: muted, centered, cleaner
- TabBar: underline active indicator for inline variant
```

---

### Task 7: Redesign Modal, Input, Select, Textarea, SearchInput, ProgressBar

**Files:**
- Modify: `frontend/src/components/ui/Modal.jsx`
- Modify: `frontend/src/components/ui/Input.jsx`
- Modify: `frontend/src/components/ui/Select.jsx`
- Modify: `frontend/src/components/ui/Textarea.jsx`
- Modify: `frontend/src/components/ui/SearchInput.jsx`
- Modify: `frontend/src/components/ui/ProgressBar.jsx`

**Step 1: Read all six files**

**Step 2: Update Modal**

Key changes: darker backdrop (black/60), card surface bg, no extra border, title in Plex Sans 600 14px:

- Change backdrop from `bg-black/50` to `bg-black/60`
- Change modal panel `bg-farm-900 border border-farm-700` to `bg-[var(--brand-card-bg)]` (no border)
- Change title font from `font-display` (mono) to `font-semibold text-sm`
- Add `shadow-[var(--brand-card-shadow)]` for light mode elevation

**Step 3: Update Input, Select, Textarea**

Key changes: use CSS variables for all colors, 6px radius (from Tailwind default), consistent sizing:

- Background: `var(--brand-input-bg)`
- Border: `var(--brand-input-border)`
- Focus border: `var(--brand-primary)`
- Label: `text-xs font-medium text-[var(--brand-text-secondary)]`

**Step 4: Update SearchInput**

- Remove background color — use bottom-border-only style for cleaner look
- Search icon: muted color
- Clear button: ghost style

**Step 5: Update ProgressBar**

- Remove colored variants — use single accent color (brand primary for normal, status colors for contextual)
- Slim down: 4px height default, 6px for lg
- Background track: `var(--brand-input-bg)`

**Step 6: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 7: Commit**

```
style(ui): redesign Modal, form inputs, SearchInput, ProgressBar

- Modal: darker backdrop, no border, refined title
- Inputs: CSS variable colors throughout
- SearchInput: bottom-border style
- ProgressBar: slimmer, single accent color
```

---

## Wave 3: Layout Shell

### Task 8: Redesign Sidebar

**Files:**
- Modify: `frontend/src/components/layout/Sidebar.jsx`

**Context:** Keep grouped structure. Refine: tighter spacing (py-1.5), lower-contrast group labels, subtle active state, fleet status as mini bar chart.

**Step 1: Read the current file**

Read: `frontend/src/components/layout/Sidebar.jsx`

**Step 2: Update NavItem component**

Tighter spacing, refined active state:

```jsx
export function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  const label = typeof children === 'string' ? children : Array.isArray(children) ? children.find(c => typeof c === 'string') : undefined
  return (
    <NavLink
      to={to}
      onClick={onClick}
      title={collapsed ? (label || undefined) : undefined}
      className={({ isActive }) => clsx(
        'transition-colors border-l-2',
        collapsed ? 'flex items-center justify-center py-1.5 rounded-sm'
                  : 'flex items-center gap-2.5 px-3 py-1.5 rounded-sm text-sm',
        isActive ? 'border-l-[var(--brand-primary)]' : 'border-l-transparent',
      )}
      style={({ isActive }) => isActive
        ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-active-text)' }
        : { color: 'var(--brand-sidebar-text)' }
      }
    >
      <Icon size={16} className="flex-shrink-0" />
      {!collapsed && <span className="font-medium text-sm">{children}</span>}
    </NavLink>
  )
}
```

**Step 3: Update NavGroup divider**

Lighter weight:

```jsx
export function NavGroup({ label, collapsed, open, onToggle }) {
  return (
    <div className="pt-3 pb-1">
      <div style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} />
      {!collapsed && (
        <button
          onClick={onToggle}
          className="flex items-center justify-between w-full px-3 mt-1.5 group"
        >
          <span className="text-[10px] uppercase font-mono font-medium"
            style={{ color: 'var(--brand-text-muted)', letterSpacing: '0.15em' }}>
            {label}
          </span>
          <ChevronDown
            size={10}
            className={clsx("transition-transform duration-200", open ? "" : "-rotate-90")}
            style={{ color: 'var(--brand-text-muted)' }}
          />
        </button>
      )}
    </div>
  )
}
```

**Step 4: Update fleet status section**

Replace colored dots with a compact horizontal bar:

```jsx
{printersData && (!collapsed || mobileOpen) && (
  <NavLink to="/printers" className="flex-shrink-0 px-3 py-2.5 hover:opacity-80 transition-opacity overflow-hidden" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} aria-label={`Fleet status: ${printersData.filter(p => isOnline(p)).length} of ${printersData.length} printers online`}>
    <div className="flex items-center justify-between mb-1.5">
      <span className="text-[10px] font-mono font-medium" style={{ color: 'var(--brand-text-muted)' }}>
        FLEET
      </span>
      <span className="text-[10px] font-mono" style={{ color: 'var(--brand-sidebar-text)' }}>
        {printersData.filter(p => isOnline(p)).length}/{printersData.length}
      </span>
    </div>
    <div className="h-1 rounded-full bg-farm-800 overflow-hidden">
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{
          width: `${printersData.length ? (printersData.filter(p => isOnline(p)).length / printersData.length * 100) : 0}%`,
          backgroundColor: 'var(--status-completed)'
        }}
      />
    </div>
  </NavLink>
)}
```

**Step 5: Update logo section padding and footer**

Tighten padding: `p-6` -> `p-4` for logo, reduce footer text size.

**Step 6: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 7: Commit**

```
style(layout): redesign Sidebar navigation

- Tighter vertical rhythm (py-1.5)
- Refined active state with 2px accent border
- Fleet status as horizontal bar chart
- Lower contrast group labels
- Reduced padding throughout
```

---

### Task 9: Update App Shell, MobileHeader, ThemeToggle

**Files:**
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/components/layout/MobileHeader.jsx`
- Modify: `frontend/src/components/layout/ThemeToggle.jsx`
- Modify: `frontend/src/components/layout/NotFound.jsx`

**Step 1: Read all four files**

**Step 2: Update App.jsx**

- Change toast styling to match new palette (use `var(--brand-card-bg)` background)
- Update desktop toolbar area: remove any decorative elements, keep search + theme + alerts minimal
- Ensure main content area uses `var(--brand-content-bg)`

**Step 3: Update MobileHeader**

- Match sidebar's refined palette
- Tighter spacing
- Use CSS variables throughout

**Step 4: Update ThemeToggle**

- Ghost button style, 32px touch target
- Smooth icon transition

**Step 5: Update NotFound**

- Match EmptyState styling
- Use muted colors

**Step 6: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 7: Commit**

```
style(layout): update App shell, MobileHeader, ThemeToggle

- Toast styling matches new palette
- MobileHeader refined with CSS variables
- ThemeToggle: ghost button, 32px target
- NotFound: matches EmptyState styling
```

---

## Wave 4: Shared Components

### Task 10: Redesign Shared Components

**Files:**
- Modify: `frontend/src/components/shared/ConfirmModal.jsx`
- Modify: `frontend/src/components/shared/DetailDrawer.jsx`
- Modify: `frontend/src/components/shared/GlobalSearch.jsx`
- Modify: `frontend/src/components/shared/KeyboardShortcutsModal.jsx`
- Modify: `frontend/src/components/shared/ProBadge.jsx`
- Modify: `frontend/src/components/shared/ProGate.jsx`
- Modify: `frontend/src/components/shared/UpgradeBanner.jsx`
- Modify: `frontend/src/components/shared/UpgradeModal.jsx`
- Modify: `frontend/src/components/shared/ErrorBoundary.jsx`

**Step 1: Read all files**

**Step 2: Update each component**

Apply consistent design language:
- `ConfirmModal`: danger variant uses red border-only button (matching new Button danger)
- `DetailDrawer`: card surface bg, no visible border in dark, subtle shadow in light. Slide-in transition
- `GlobalSearch`: input uses bottom-border style. Results use card surface. Active result has subtle highlight
- `KeyboardShortcutsModal`: mono font for key labels, muted colors for descriptions
- `ProBadge`: subtle — text-xs, amber color from brand-primary, no background
- `ProGate`: blur overlay uses card surface bg at 80% opacity
- `UpgradeBanner`: secondary button style, muted messaging
- `UpgradeModal`: follows modal redesign
- `ErrorBoundary`: muted error display, ghost retry button

**Step 3: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 4: Commit**

```
style(shared): redesign shared components

- ConfirmModal, DetailDrawer, GlobalSearch, ProBadge, ProGate
- UpgradeBanner, UpgradeModal, ErrorBoundary, KeyboardShortcutsModal
- All use CSS variables and refined design language
```

---

## Wave 5: Printer Components

### Task 11: Create SpoolRing SVG Component

**Files:**
- Create: `frontend/src/components/ui/SpoolRing.jsx`

**Context:** New component — circular arc showing filament color, material, and remaining level. This replaces the colored rectangles in printer cards.

**Step 1: Create the SpoolRing component**

```jsx
import clsx from 'clsx'

export default function SpoolRing({ color = '#888', material = '', level = 100, empty = false, size = 20, warning = false, className }) {
  const strokeWidth = 3
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (Math.min(100, Math.max(0, level)) / 100) * circumference
  const isLow = level < 15

  if (empty) {
    return (
      <div className={clsx('inline-flex items-center gap-1.5', className)}>
        <svg width={size} height={size} className="flex-shrink-0">
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="var(--brand-text-muted)"
            strokeWidth={strokeWidth}
            strokeDasharray="3 3"
            opacity={0.4}
          />
        </svg>
        <span className="text-xs text-[var(--brand-text-muted)]">Empty</span>
      </div>
    )
  }

  return (
    <div className={clsx('inline-flex items-center gap-1.5', className)}>
      <svg width={size} height={size} className="flex-shrink-0 -rotate-90">
        {/* Background track */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--brand-input-bg)"
          strokeWidth={strokeWidth}
        />
        {/* Fill arc */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className="transition-all duration-500"
        />
        {/* Warning ring */}
        {isLow && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius + 2}
            fill="none"
            stroke="var(--status-warning)"
            strokeWidth={1}
            strokeDasharray="2 2"
            opacity={0.6}
          />
        )}
      </svg>
      {material && (
        <span className={clsx(
          'text-xs',
          isLow ? 'text-[var(--status-warning)]' : 'text-[var(--brand-text-secondary)]'
        )}>
          {material}
        </span>
      )}
    </div>
  )
}
```

**Step 2: Export from ui/index.js**

Add `export { default as SpoolRing } from './SpoolRing'` to the barrel export.

**Step 3: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 4: Commit**

```
feat(ui): add SpoolRing component for filament visualization

- Circular arc SVG showing filament color and fill level
- Dashed ring for empty slots
- Warning treatment below 15% remaining
- Material label with contextual color
```

---

### Task 12: Redesign PrinterCard and FilamentSlotEditor

**Files:**
- Modify: `frontend/src/components/printers/PrinterCard.jsx`
- Modify: `frontend/src/components/printers/FilamentSlotEditor.jsx`

**Step 1: Read both files**

**Step 2: Update PrinterCard**

- Replace colored filament rectangles with SpoolRing components
- Use card surface (no border in dark mode)
- Status dot: 6px, no glow
- Tab links at bottom: plain text, no pill badges, underline on active
- Temperature display: mono font, no icon prefix
- Progress bar: slim 4px, accent color
- Printer name: section heading weight (Plex Sans 600 14px)
- Model name: caption text (11px, muted)

**Step 3: Update FilamentSlotEditor**

- Replace colored rectangles with SpoolRing in the editor view
- Cleaner form layout using updated Input/Select components
- Use card surface colors

**Step 4: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 5: Commit**

```
style(printers): redesign PrinterCard with SpoolRing indicators

- Filament slots use circular arc spool rings
- Card surface without borders in dark mode
- Refined status dots (no glow)
- Mono font for temperature/data values
- Slim progress bar
```

---

### Task 13: Redesign Remaining Printer Components

**Files:**
- Modify: `frontend/src/components/printers/PrinterModal.jsx`
- Modify: `frontend/src/components/printers/PrinterPanels.jsx`
- Modify: `frontend/src/components/printers/NozzleStatusCard.jsx`
- Modify: `frontend/src/components/printers/HmsHistoryPanel.jsx`
- Modify: `frontend/src/components/printers/AmsEnvironmentChart.jsx`
- Modify: `frontend/src/components/printers/PrinterTelemetryChart.jsx`
- Modify: `frontend/src/components/printers/EmergencyStop.jsx`

**Step 1: Read all files**

**Step 2: Update each**

- `PrinterModal`: follows updated Modal + form input styling
- `PrinterPanels`: use SpoolRing in FilamentSlotsPanel, card surface colors
- `NozzleStatusCard`: mono font for temps, muted layout, no colored backgrounds
- `HmsHistoryPanel`: table-industrial styling, muted severity badges
- `AmsEnvironmentChart`: chart colors per design (grid: `#1A1D25`, axis: muted, no gradient fills)
- `PrinterTelemetryChart`: same chart treatment
- `EmergencyStop`: keep prominent but use danger button styling (red border-only default, solid on hover/active)

**Step 3: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 4: Commit**

```
style(printers): redesign printer modals, panels, charts

- PrinterModal, NozzleStatusCard, HmsHistoryPanel updated
- Charts use refined palette (subtle grid, muted axis)
- EmergencyStop uses danger button variant
- PrinterPanels use SpoolRing component
```

---

## Wave 6: Camera System

### Task 14: Redesign Camera Grid Page

**Files:**
- Modify: `frontend/src/pages/printers/Cameras.jsx`

**Step 1: Read the current file**

**Step 2: Update CameraCard component**

- Card: `bg-[var(--brand-card-bg)]` rounded-md, no visible border in dark mode
- Video fills card edges (remove internal padding around stream)
- Status overlay (bottom-left): `LIVE` in 10px Plex Mono, white/70%, 6px green dot (no pulsing)
- Controls overlay (top-right, hover): `backdrop-blur-sm bg-black/40` buttons, 6px radius
- Name bar below video: 28px height, left-aligned name, right-aligned status text

**Step 3: Update grid layout toggle**

Replace 3 icon buttons with segmented control:

```jsx
<div className="flex rounded-md border border-[var(--brand-card-border)] overflow-hidden">
  {[1, 2, 3].map(n => (
    <button
      key={n}
      onClick={() => setColumns(n)}
      className={clsx(
        'px-2.5 py-1 text-xs font-mono transition-colors',
        columns === n
          ? 'bg-[var(--brand-primary)] text-white'
          : 'text-[var(--brand-text-secondary)] hover:bg-farm-800'
      )}
    >
      {n === 1 ? '1×1' : n === 2 ? '2×2' : '3×3'}
    </button>
  ))}
</div>
```

**Step 4: Update Control Room mode**

- Header: 40px height, `text-sm font-semibold` (not font-display font-bold text-lg)
- Camera tiles: `gap-0` with `border border-[var(--brand-card-border)]` (1px separation)
- No rounded corners on tiles
- Camera names: `text-[10px] font-mono` overlaid bottom-left
- Connection status: small colored dot top-right only (no text)
- Clock: `font-mono text-base` (not text-xl)
- Exit button: text-only ghost button "Exit" (no icon)

**Step 5: Update PipPlayer**

- 6px radius, 1px border, subtle shadow
- Header: 24px, `bg-[var(--brand-card-bg)]/90 backdrop-blur-sm`
- Replace Move icon with 4-dot grip pattern (use `GripVertical` from Lucide)
- LIVE indicator: bottom-left, consistent with grid view

**Step 6: Update camera settings panel**

- Matches card surface design
- Toggle buttons use secondary/ghost variants

**Step 7: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 8: Commit**

```
style(cameras): redesign Camera grid, Control Room, PiP

- Camera cards: borderless, frosted glass controls on hover
- Grid toggle: segmented control (1x1 / 2x2 / 3x3)
- Control Room: slim 40px toolbar, zero-gap tiles, no rounded corners
- PiP: 6px radius, 24px header, grip icon, backdrop-blur
- Consistent LIVE/status indicators across all views
```

---

### Task 15: Redesign CameraDetail and CameraModal

**Files:**
- Modify: `frontend/src/pages/printers/CameraDetail.jsx`
- Modify: `frontend/src/components/printers/CameraModal.jsx`

**Step 1: Read both files**

**Step 2: Update CameraDetail**

- Video container: zero radius, full bleed to edges, 1px bottom border
- Breadcrumb: `ArrowLeft` as ghost button + `/` divider + printer name in heading weight
- Info panels (right column): borderless sections, thin dividers, caption-style titles (11px, uppercase, muted)
- LIVE badge: top-left, 6px dot + mono text (consistent)
- Snapshot button: hover-reveal, frosted glass, top-right

**Step 3: Update CameraModal**

- Small PIP mode: matches PipPlayer redesign from Task 14
- Large modal: video fills modal content area, status bar below (name left, status right, mono text). Info bar only visible when printing
- Fullscreen mode: video full viewport, side panel (300px, `bg-[var(--brand-card-bg)]`, `border-l border-[var(--brand-card-border)]`). Borderless sections inside panel with thin dividers
- Remove size-cycle expand/minimize buttons — use a single toggle for panel show/hide
- AiIndicator: subtle — smaller, `text-[10px]`, purple dot only (no pill background)

**Step 4: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 5: Commit**

```
style(cameras): redesign CameraDetail and CameraModal

- CameraDetail: full-bleed video, refined breadcrumb, info panels
- CameraModal: clean PIP/modal/fullscreen modes
- Consistent LIVE indicators and frosted glass controls
- AiIndicator simplified
```

---

## Wave 7: Notification Alert Types

### Task 16: Replace Emoji with Lucide Icons in NotificationsTab

**Files:**
- Modify: `frontend/src/components/admin/NotificationsTab.jsx`

**Step 1: Read the file, find the alertTypeLabels object**

**Step 2: Replace emoji icons with Lucide components**

```javascript
import { CheckCircle, XCircle, AlertTriangle, Wrench, FileText, XOctagon, Thermometer, ListPlus, SkipForward } from 'lucide-react'

const alertTypeLabels = {
  'print_complete':    { label: 'Print Complete',     icon: CheckCircle,    color: 'var(--status-completed)' },
  'print_failed':      { label: 'Print Failed',       icon: XCircle,        color: 'var(--status-failed)' },
  'spool_low':         { label: 'Spool Low',          icon: AlertTriangle,  color: 'var(--status-warning)' },
  'maintenance_overdue': { label: 'Maintenance Due',  icon: Wrench,         color: 'var(--brand-text-muted)' },
  'job_submitted':     { label: 'Job Submitted',      icon: FileText,       color: 'var(--brand-text-muted)' },
  'job_approved':      { label: 'Job Approved',       icon: CheckCircle,    color: 'var(--status-printing)' },
  'job_rejected':      { label: 'Job Rejected',       icon: XOctagon,       color: 'var(--status-failed)' },
  'bed_cooled':        { label: 'Bed Cooled',         icon: Thermometer,    color: 'var(--status-printing)' },
  'queue_added':       { label: 'Job Queued',         icon: ListPlus,       color: 'var(--brand-text-muted)' },
  'queue_skipped':     { label: 'Job Skipped',        icon: SkipForward,    color: 'var(--brand-text-muted)' },
  'queue_failed_start': { label: 'Job Failed to Start', icon: AlertTriangle, color: 'var(--status-failed)' },
}
```

Update all rendering code that displays `alertType.icon` — change from rendering a string emoji to rendering the Lucide component:

```jsx
// Old: <span>{type.icon}</span>
// New: <type.icon size={14} style={{ color: type.color }} />
```

**Step 3: Build and verify**

Run: `cd /workspace/odin/frontend && npm run build`

**Step 4: Commit**

```
style(notifications): replace emoji alert icons with Lucide components

- All 11 alert types now use Lucide React icons
- Icons colored by semantic status
- Removed all emoji from UI
```

---

## Wave 8: Page Components — Dashboard & Core

### Task 17: Redesign Dashboard Page

**Files:**
- Modify: `frontend/src/pages/dashboard/Dashboard.jsx`

**Step 1: Read the file**

**Step 2: Apply design system**

- StatCards: use left-border accent variant, mono values
- Printer overview cards: match PrinterCard redesign (SpoolRing, no glow dots, card surface)
- Section headings: Plex Sans 600 14px
- Quick action buttons: ghost variant
- All colors via CSS variables

**Step 3: Build and verify**

**Step 4: Commit**

```
style(dashboard): redesign Dashboard page

- StatCards with left-border accents
- Printer cards use SpoolRing indicators
- Refined typography and spacing
```

---

### Task 18: Redesign TVDashboard, Overlay, SectionErrorBoundary

**Files:**
- Modify: `frontend/src/pages/dashboard/TVDashboard.jsx`
- Modify: `frontend/src/pages/dashboard/Overlay.jsx`
- Modify: `frontend/src/pages/dashboard/SectionErrorBoundary.jsx`

**Step 1: Read all files**

**Step 2: Update**

- TVDashboard: same treatment as Control Room — slim header, clean grid, mono data values
- Overlay: dark surface, minimal chrome, mono fonts for data
- SectionErrorBoundary: muted error display

**Step 3: Build, commit**

```
style(dashboard): redesign TV, Overlay, error boundary

- TVDashboard matches Control Room aesthetic
- Overlay: minimal dark chrome
- SectionErrorBoundary: muted styling
```

---

## Wave 9: Job & Inventory Components

### Task 19: Redesign Job Components

**Files:**
- Modify: `frontend/src/components/jobs/JobRow.jsx`
- Modify: `frontend/src/components/jobs/JobTableHeader.jsx`
- Modify: `frontend/src/components/jobs/JobModals.jsx`
- Modify: `frontend/src/components/jobs/FailureReasonModal.jsx`
- Modify: `frontend/src/components/jobs/RecentlyCompleted.jsx`
- Modify: `frontend/src/pages/jobs/Jobs.jsx`
- Modify: `frontend/src/pages/jobs/Upload.jsx`
- Modify: `frontend/src/pages/jobs/Timeline.jsx`

**Step 1: Read all files**

**Step 2: Update**

- JobRow: table-industrial styling, StatusBadge inline variant, mono data values
- JobTableHeader: sentence case (no uppercase), font-semibold, sortable chevrons
- JobModals: follow Modal + form input redesign
- Jobs page: tab bar uses inline underline variant
- Upload page: drop zone with dashed border, muted styling
- Timeline: grid lines match chart palette, job cards use brightness hover

**Step 3: Build, commit**

```
style(jobs): redesign job components and pages

- JobRow, JobTableHeader, modals updated
- Jobs page: inline tab bar
- Upload: refined drop zone
- Timeline: chart-consistent grid lines
```

---

### Task 20: Redesign Inventory Components

**Files:**
- Modify: `frontend/src/components/inventory/SpoolCard.jsx`
- Modify: `frontend/src/components/inventory/SpoolGrid.jsx`
- Modify: `frontend/src/components/inventory/SpoolModals.jsx`
- Modify: `frontend/src/components/inventory/SpoolEditModals.jsx`
- Modify: `frontend/src/components/inventory/FilamentLibraryView.jsx`
- Modify: `frontend/src/components/inventory/QRScannerModal.jsx`
- Modify: `frontend/src/pages/inventory/Spools.jsx`
- Modify: `frontend/src/pages/inventory/Consumables.jsx`

**Step 1: Read all files**

**Step 2: Update**

- SpoolCard: use SpoolRing for the filament visual. Card surface, weight as slim progress bar
- SpoolGrid: consistent gap/padding
- SpoolModals, SpoolEditModals: follow Modal + form input redesign
- FilamentLibraryView: table-industrial, color swatches as small 12px circles
- QRScannerModal: dark surface, minimal chrome
- Spools page: card surface filter bar, ghost action buttons
- Consumables page: same treatment

**Step 3: Build, commit**

```
style(inventory): redesign spool cards, modals, filament library

- SpoolCard uses SpoolRing indicators
- Modals follow updated design system
- FilamentLibraryView: refined table styling
```

---

## Wave 10: Orders, Models, Auth Components

### Task 21: Redesign Order Components and Pages

**Files:**
- Modify: `frontend/src/components/orders/OrderTable.jsx`
- Modify: `frontend/src/components/orders/OrderModals.jsx`
- Modify: `frontend/src/pages/orders/Orders.jsx`
- Modify: `frontend/src/pages/orders/Products.jsx`
- Modify: `frontend/src/pages/orders/Calculator.jsx`

**Step 1: Read, update, build, commit**

- OrderTable: table-industrial, StatusBadge badge variant for order status
- OrderModals: Modal + form input redesign
- Products: card grid with card surface bg
- Calculator: mono font for numerical outputs, card sections

```
style(orders): redesign order table, modals, products, calculator

- Table-industrial styling throughout
- StatusBadge badge variant for order status
- Calculator: mono numerical display
```

---

### Task 22: Redesign Model Pages and Components

**Files:**
- Modify: `frontend/src/components/models/ModelViewer.jsx`
- Modify: `frontend/src/components/models/ModelRevisionPanel.jsx`
- Modify: `frontend/src/pages/models/Models.jsx`
- Modify: `frontend/src/pages/models/Projects.jsx`
- Modify: `frontend/src/pages/models/Profiles.jsx`

**Step 1: Read, update, build, commit**

- ModelViewer: dark canvas bg, ghost control buttons, minimal toolbar
- ModelRevisionPanel: list with thin dividers, mono timestamps
- Models page: card grid, filter bar
- Projects page: card surface styling
- Profiles page: table-industrial

```
style(models): redesign model viewer, revision panel, pages

- ModelViewer: dark canvas, ghost controls
- Profiles: table-industrial styling
```

---

### Task 23: Redesign Auth Pages

**Files:**
- Modify: `frontend/src/pages/auth/Login.jsx`
- Modify: `frontend/src/pages/auth/Setup.jsx`
- Modify: `frontend/src/pages/auth/ResetPassword.jsx`
- Modify: `frontend/src/components/auth/MFASetup.jsx`
- Modify: `frontend/src/components/auth/SSOButton.jsx`
- Modify: `frontend/src/components/auth/SessionManager.jsx`
- Modify: `frontend/src/components/auth/APITokenManager.jsx`

**Step 1: Read, update, build, commit**

- Login: centered card, refined form inputs, primary submit button, muted links
- Setup wizard: keep dark theme override (already scoped via .setup-wizard CSS), refine spacing
- ResetPassword: matches Login styling
- MFASetup: clean card, mono for backup codes
- SSOButton: secondary button variant
- APITokenManager: table-industrial, mono for token values

```
style(auth): redesign login, setup, auth components

- Login: centered card with refined inputs
- Setup wizard: tighter spacing
- APITokenManager: table-industrial
```

---

## Wave 11: Admin & Settings

### Task 24: Redesign Admin/Settings Components

**Files:**
- Modify: `frontend/src/pages/admin/Settings.jsx`
- Modify: `frontend/src/pages/admin/Admin.jsx`
- Modify: `frontend/src/pages/admin/Branding.jsx`
- Modify: `frontend/src/pages/admin/Permissions.jsx`
- Modify: `frontend/src/pages/admin/AuditLogs.jsx`
- Modify: `frontend/src/components/admin/GeneralTab.jsx`
- Modify: `frontend/src/components/admin/SystemTab.jsx`
- Modify: `frontend/src/components/admin/VisionSettingsTab.jsx`
- Modify: `frontend/src/components/admin/LicenseTab.jsx`

**Step 1: Read, update, build, commit**

- Settings page: TabBar inline variant for tab navigation
- Admin page: same
- Branding page: card sections, form inputs match system
- Permissions: table-industrial for permission matrix
- AuditLogs: table-industrial, mono timestamps
- All tabs: card sections with thin dividers, consistent form styling
- LicenseTab: mono for license keys, status indicator

```
style(admin): redesign settings and admin pages

- TabBar inline variant throughout
- Table-industrial for data displays
- Consistent card section layout
```

---

### Task 25: Redesign Remaining Admin Components

**Files:**
- Modify: `frontend/src/components/admin/OrgManager.jsx`
- Modify: `frontend/src/components/admin/GroupManager.jsx`
- Modify: `frontend/src/components/admin/OIDCSettings.jsx`
- Modify: `frontend/src/components/admin/WebhookSettings.jsx`
- Modify: `frontend/src/components/admin/IPAllowlistSettings.jsx`
- Modify: `frontend/src/components/admin/ReportScheduleManager.jsx`
- Modify: `frontend/src/components/admin/QuotaManager.jsx`
- Modify: `frontend/src/components/admin/DataRetentionSettings.jsx`
- Modify: `frontend/src/components/admin/BackupRestore.jsx`
- Modify: `frontend/src/components/admin/LogViewer.jsx`
- Modify: `frontend/src/components/admin/ChargebackReport.jsx`

**Step 1: Read, update, build, commit**

Apply consistent design system to all admin components:
- Card sections with consistent padding
- Form inputs via updated Input/Select/Textarea
- Action buttons: primary for main action, ghost for secondary
- Tables: table-industrial
- Mono font for technical values (IPs, URLs, timestamps, keys)

```
style(admin): redesign org, webhook, quota, and utility admin components

- All admin components use consistent design system
- Mono font for technical values
- Table-industrial for lists
```

---

## Wave 12: Analytics, Archives, Remaining Pages

### Task 26: Redesign Analytics Pages

**Files:**
- Modify: `frontend/src/pages/analytics/Analytics.jsx`
- Modify: `frontend/src/pages/analytics/Utilization.jsx`
- Modify: `frontend/src/pages/analytics/PrintLog.jsx`
- Modify: `frontend/src/pages/analytics/EducationReports.jsx`
- Modify: `frontend/src/components/reporting/EnergyWidget.jsx`

**Step 1: Read, update, build, commit**

- Charts: grid `#1A1D25`, axis `#3D4559`, no gradient fills, solid colors at low opacity for area charts
- Tooltip: card surface bg, subtle shadow, no border
- Chart card containers: card surface bg
- PrintLog: table-industrial
- Mono font for all numerical data
- EnergyWidget: matches chart palette

```
style(analytics): redesign analytics pages and charts

- Recharts: subtle grid, muted axis, solid fill colors
- PrintLog: table-industrial
- Mono numerical data throughout
```

---

### Task 27: Redesign Archive, Timelapse, Alert, Detection Pages

**Files:**
- Modify: `frontend/src/pages/archives/Archives.jsx`
- Modify: `frontend/src/pages/archives/Timelapses.jsx`
- Modify: `frontend/src/pages/notifications/Alerts.jsx`
- Modify: `frontend/src/pages/vision/Detections.jsx`
- Modify: `frontend/src/components/notifications/AlertBell.jsx`
- Modify: `frontend/src/components/vision/DetectionFeed.jsx`

**Step 1: Read, update, build, commit**

- Archives: card grid, card surface, ghost action buttons
- Timelapses: video player with dark surface, minimal controls
- Alerts: filter bar, list items with StatusBadge inline, Lucide icons (not emoji)
- Detections: detection cards with muted severity badges, image thumbnails
- AlertBell: dropdown matches card surface, unread count uses accent color
- DetectionFeed: compact list, muted styling

```
style(pages): redesign archives, alerts, detections, timelapse

- Card grid layouts throughout
- StatusBadge inline for alert types
- Muted severity indicators
```

---

## Wave 13: Printer Pages

### Task 28: Redesign Printers, PrinterDetail, Maintenance Pages

**Files:**
- Modify: `frontend/src/pages/printers/Printers.jsx`
- Modify: `frontend/src/pages/printers/PrinterDetail.jsx`
- Modify: `frontend/src/pages/printers/Maintenance.jsx`

**Step 1: Read, update, build, commit**

- Printers: card grid using redesigned PrinterCard, filter bar, ghost add button
- PrinterDetail: sections with thin dividers, SpoolRing in filament display, mono temps
- Maintenance: card sections for schedule items, date/interval in mono

```
style(printers): redesign Printers, PrinterDetail, Maintenance pages

- Card grid with redesigned PrinterCards
- PrinterDetail: sectioned layout with thin dividers
- Maintenance: card sections, mono dates
```

---

## Wave 14: BrandingContext Update

### Task 29: Update BrandingContext for New Palette

**Files:**
- Modify: `frontend/src/BrandingContext.jsx`

**Step 1: Read the file**

**Step 2: Update default branding values**

Ensure the default branding values match the new palette:
- Default primary: `#C47A1A` (was `#d97706`)
- Default accent: `#D4891F` (was `#f59e0b`)
- Update any hardcoded color references in the font loading or branding application logic

**Step 3: Build, commit**

```
style(branding): update BrandingContext defaults for refined palette

- Default accent colors updated
- Consistent with new CSS variable defaults
```

---

## Wave 15: Light Mode Verification Pass

### Task 30: Light Mode Audit and Fixes

**Files:**
- Modify: `frontend/src/index.css` (light mode overrides section)
- Potentially modify: any component with hardcoded dark-only colors

**Step 1: Build and run dev server**

Run: `cd /workspace/odin/frontend && npm run dev`

**Step 2: Switch to light mode, audit every page**

Walk through:
- Dashboard, Printers, Cameras, Jobs, Timeline
- Models, Spools, Orders, Analytics
- Settings, Admin, Login

For each page verify:
- Text is readable (sufficient contrast)
- Cards have visible shadows
- Status colors are correct
- No invisible elements (white on white)
- Forms inputs are visible

**Step 3: Fix any issues found**

Most should be handled by the CSS variable system. Fix any remaining hardcoded colors.

**Step 4: Commit**

```
style(theme): light mode audit and fixes

- Fixed contrast issues
- Verified all pages in both themes
```

---

## Wave 16: Final Polish

### Task 31: Consistency Sweep

**Files:**
- Any files with remaining inconsistencies

**Step 1: Search for remaining old patterns**

Look for:
- Any remaining `bg-farm-900` that should be `bg-[var(--brand-card-bg)]`
- Any remaining `border-farm-800` that should be `border-[var(--brand-card-border)]`
- Any remaining `text-print-400` that should use the new color scale
- Any remaining emoji characters
- Any remaining `translate-y` hover effects
- Any remaining glow box-shadows on status dots

Run targeted greps for each pattern.

**Step 2: Fix all remaining instances**

**Step 3: Full build**

Run: `cd /workspace/odin/frontend && npm run build`
Expected: Clean build, no warnings.

**Step 4: Commit**

```
style(cleanup): final consistency sweep

- Replaced remaining hardcoded farm colors with CSS variables
- Removed last emoji references
- Removed remaining glow effects
- Consistent hover treatments
```

---

### Task 32: Run Full Test Suite

**Step 1: Build the Docker container**

Run: `cd /workspace/odin && make build`

**Step 2: Run main + RBAC tests**

Run: `cd /workspace/odin && make test`
Expected: All ~1966 tests pass. This is a visual-only change so no functional tests should break.

**Step 3: If any tests fail, diagnose and fix**

Visual changes should not affect API tests. If frontend-specific tests exist (component tests), check if they assert on class names or styles that changed.

**Step 4: Final commit**

```
style(ui): complete O.D.I.N. UI redesign

- 14-section design system applied across 122 frontend files
- Refined warm industrial palette (desaturated amber)
- SpoolRing filament indicators replacing colored rectangles
- All emoji replaced with Lucide icons
- Camera views consistent across 5 modes
- Both dark and light modes polished
- All tests passing
```

---

## Summary

| Wave | Tasks | Scope |
|------|-------|-------|
| 1: Foundation | 1-2 | CSS variables, Tailwind config |
| 2: UI Primitives | 3-7 | Button, Card, StatusBadge, StatCard, PageHeader, EmptyState, TabBar, Modal, inputs, ProgressBar |
| 3: Layout Shell | 8-9 | Sidebar, App.jsx, MobileHeader, ThemeToggle |
| 4: Shared | 10 | ConfirmModal, DetailDrawer, GlobalSearch, ProBadge, etc. |
| 5: Printers | 11-13 | SpoolRing, PrinterCard, FilamentSlotEditor, printer components |
| 6: Cameras | 14-15 | Camera grid, Control Room, PiP, CameraDetail, CameraModal |
| 7: Notifications | 16 | Replace emoji with Lucide icons |
| 8: Dashboard | 17-18 | Dashboard, TVDashboard, Overlay |
| 9: Jobs & Inventory | 19-20 | Job components, spool components, pages |
| 10: Orders/Models/Auth | 21-23 | Order, model, auth components and pages |
| 11: Admin | 24-25 | Settings, admin tabs, managers |
| 12: Analytics/Archives | 26-27 | Charts, analytics, archives, alerts, detections |
| 13: Printer Pages | 28 | Printers list, detail, maintenance |
| 14: Branding | 29 | BrandingContext defaults |
| 15: Light Mode | 30 | Cross-theme audit |
| 16: Polish | 31-32 | Consistency sweep, full test run |

**Total: 32 tasks across 16 waves. ~122 files modified.**

**Critical path:** Wave 1 (tokens) -> Wave 2 (primitives) -> everything else. Waves 3+ can be parallelized where there's no file overlap.
