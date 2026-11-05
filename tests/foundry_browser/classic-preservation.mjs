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
  // Restore the explicit native tag carried by every numeric presentation adapter.
  s = s.replace(/<PrinterNumber as="(span|p|td|dd)"([\s\S]*?)<\/PrinterNumber>/g, '<$1$2</$1>')
  const sf = ts.createSourceFile(file, s, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  return ts.createPrinter({ removeComments: true }).printFile(sf)
}
export const digest = s => createHash('sha256').update(s).digest('hex')
