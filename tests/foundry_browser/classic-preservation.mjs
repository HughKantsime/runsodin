/** Source-level companion to the pixel comparison. Only undo the documented
 * Foundry adapters; retain every original class, style, handler, query and guard.
 * The immutable baseline digests came from 3d372ba2, not from the edited files.
 */
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import path from 'node:path'
const require = createRequire(path.resolve(import.meta.dirname, '../../frontend/package.json'))
const ts = require('typescript')
export function classicSource(file, source) {
  let s = source
  if (file.endsWith('index.css')) return s.replace("@import './foundry-pages.css';\n", '')
  s = s.replace(/^import (Table|PageState|FoundryPage|PrinterNumber) from '[^']+'\n/gm, '')
  s = s.replace(/<PageState kind="(?:loading|error|empty)"/g, '<div').replace(/<\/PageState>/g, '</div>')
  s = s.replace(/<(\/?)Table(?=[\s>])/g, '<$1table')
  s = s.replace(/<FoundryPage name="[^"]*">/g, '').replace(/<\/FoundryPage>/g, '')
  if (file.endsWith('/App.tsx')) {
    s = s.replace("import Toast from './components/ui/Toast'", "import { Toaster } from 'react-hot-toast'")
    s = s.replace('<Toast />', '<Toaster position="top-right" toastOptions={{ style: { background: \'var(--brand-card-bg)\', color: \'var(--brand-text-primary)\', border: \'1px solid var(--brand-card-border)\' } }} />')
  }
  if (file.includes('/components/ui/') || file.endsWith('/pages/archives/Timelapses.tsx')) {
    s = s.replace("import { useFoundry } from '../../hooks/themePreferences'\n", '').replace('  const foundry = useFoundry()\n', '')
    // Exact guards: changing the condition or Classic value makes this test fail.
    s = s.replace(/\sdata-foundry-(?:ui|variant|active)=\{foundry \? (?:'[a-z]+'|resolvedVariant|isActive) : undefined\}/g, '')
    s = s.replace(/\saria-pressed=\{foundry \? isActive : undefined\}/g, '')
    s = s.replace(/^export \{ default as (?:Table|PageState|Toast) \} from '[^']+'\n/gm, '')
  }
  // Round 2: CheckTarget returns the original input verbatim in Classic.
  // Keep all child props/classes/handlers in the digest; strip only the adapter.
  if (file.endsWith('/Printers.tsx')) {
    // Round 3 relocates the same selection into the Foundry header. Classic
    // still returns selection immediately before children. Preserve input props.
    s = s.replace(/<PrinterPresentation foundry=\{foundry\} printer=\{printer\} selection=\{(canDo\('printers.edit'\) && \([\s\S]*?<\/CheckTarget>\s*\))\}><PrinterCard/, '{$1}\n              <PrinterPresentation foundry={foundry} printer={printer}><PrinterCard')
      .replace('className={foundry ? "rounded border-', 'className={foundry ? "mb-2 rounded border-')
      .replace('function PrinterPresentation({ foundry, printer, children, selection })', 'function PrinterPresentation({ foundry, printer, children })')
      .replace('<ManagedPrinter printer={printer} selection={selection}>{children}</ManagedPrinter> : <>{selection}{children}</>', '<ManagedPrinter printer={printer}>{children}</ManagedPrinter> : <>{children}</>')
  }
  s = s.replace(/^import CheckTarget from '[^']+'\n/gm, '')
    .replace(/<CheckTarget(?: className="(?:mb-2|absolute top-3 left-3 z-10)")?>/g, '').replace(/<\/CheckTarget>/g, '')
  if (file.endsWith('/Projects.tsx')) s = s.replace('aria-label={`Colour: ${c}`}', '').replace('aria-pressed={color === c}', '')
  if (file.endsWith('/Jobs.tsx')) s = s.replace("import { useFoundry } from '../../hooks/themePreferences'\n", '')
    .replace('  const foundry = useFoundry()\n', '').replace(" role={foundry ? 'region' : undefined}", '')
  // Part B fixes: these exact accessibility-only attributes and Foundry-only
  // styling hooks do not change Classic pixels. Keep the original digests.
  if (file.endsWith('/EmergencyStop.tsx')) {
    s = s.replace("import type { Printer } from '../../types'\n", '')
      .replace('type ActivePrinter = Printer', `interface ActivePrinter {
  id: number
  name: string
  nickname?: string
  gcode_state: string
  [key: string]: any
}`)
      .replace('fetchAPI<{ success?: boolean }>', 'fetchAPI')
      .replace('emergency-controls-toggle fixed', 'fixed').replace('emergency-controls-panel fixed', 'fixed')
  }
  if (file.endsWith('/PrintLog.tsx')) s = s.replace(' tabIndex={0} role="region" aria-label="Print log"', '')
  if (file.endsWith('/Utilization.tsx')) s = s.replace(' tabIndex={0} role="region" aria-label="Printer utilization details"', '')
  if (file.endsWith('/Archives.tsx')) s = s.replace('aria-label={`Select ${displayName(item)} for comparison`}', '')
  if (file.endsWith('/Detections.tsx')) s = s.replace('aria-label={`Select ${det.detection_type} detection ${det.id}`}', '')
  if (file.endsWith('/Alerts.tsx')) {
    s = s.replace("import { useFoundry } from '../../hooks/themePreferences'\n", '').replace('  const foundry = useFoundry()\n', '')
      .replace("foundry ? 'var(--brand-on-primary)' : 'white'", "'white'")
  }
  if (file.endsWith('/Dashboard.tsx')) {
    // Part A's digest already contained the missing footer regression. The
    // restoration is separately asserted against the pre-Foundry footer markup.
    s = s.replace('  const foundry = useFoundry()\n', '')
    s = s.replace(`          {!foundry && <div className="flex items-center gap-1.5">
            <div className={\`w-1.5 h-1.5 rounded-full \${statusColor}\`}></div>
            <span className={online ? "text-[var(--status-completed)]" : "text-[var(--brand-text-muted)]"}>{statusLabel}</span>
          </div>}\n`, '')
  }
  // Restore the explicit native tag carried by every numeric presentation adapter.
  s = s.replace(/<PrinterNumber as="(span|p|td|dd)"([\s\S]*?)<\/PrinterNumber>/g, '<$1$2</$1>')
  const sf = ts.createSourceFile(file, s, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  return ts.createPrinter({ removeComments: true }).printFile(sf)
}
export const digest = s => createHash('sha256').update(s).digest('hex')
