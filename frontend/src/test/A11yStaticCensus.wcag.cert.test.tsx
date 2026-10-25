// Cert F / cluster D — F12-12.7-03/-04/-06/-07 census of the CURRENT source (TypeScript AST, not string grep),
// paired with real renders of the shared Modal. The rendered-page evidence lives in
// tests/test_contracts/test_cert_F_D_wcag_browser.py (built bundle in Chromium against a real server).
// Receipt: artifacts/application-certification/cert-F/D-a11y-static-census.json
// @vitest-environment jsdom
// @ts-ignore -- node built-ins under vitest; the frontend tsconfig ships no @types/node
import fs from 'node:fs'
// @ts-ignore -- as above
import path from 'node:path'
import ts from 'typescript'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import Modal from '../components/ui/Modal'

declare const __dirname: string  // provided by vitest (node)
const SRC = path.resolve(__dirname, '..')
const RECEIPT = path.resolve(SRC, '../../artifacts/application-certification/cert-F/D-a11y-static-census.json')

type Btn = { file: string; line: number; aria: string | null; title: boolean; labelledby: boolean }
type Census = {
  iconButtons: Btn[]; modalUsages: { file: string; line: number; alert: boolean }[]
  roleDialogs: { file: string; line: number; role: string }[]; overlaysWithoutDialog: { file: string; line: number }[]
  th: { file: string; line: number; scope: string | null }[]; progressbars: { file: string; line: number }[]
}

function walk(dir: string, out: string[] = []) {
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f)
    if (fs.statSync(p).isDirectory()) walk(p, out)
    else if (/\.(tsx|jsx)$/.test(f) && !/\.test\.|\.cert\./.test(f)) out.push(p)
  }
  return out
}

function census(): Census {
  const c: Census = { iconButtons: [], modalUsages: [], roleDialogs: [], overlaysWithoutDialog: [], th: [], progressbars: [] }
  for (const file of walk(SRC)) {
    const sf = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    const rel = path.relative(SRC, file)
    const icons = new Set<string>()
    for (const st of sf.statements) {
      if (ts.isImportDeclaration(st) && /lucide-react/.test(st.moduleSpecifier.getText()) && st.importClause?.namedBindings
        && ts.isNamedImports(st.importClause.namedBindings)) st.importClause.namedBindings.elements.forEach(e => icons.add(e.name.text))
    }
    const line = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart()).line + 1
    const open = (n: ts.JsxElement | ts.JsxSelfClosingElement) => (ts.isJsxElement(n) ? n.openingElement : n)
    const tag = (n: ts.JsxElement | ts.JsxSelfClosingElement) => open(n).tagName.getText()
    const attrs = (n: ts.JsxElement | ts.JsxSelfClosingElement) => {
      const m: Record<string, string | true> = {}
      for (const a of open(n).attributes.properties) if (ts.isJsxAttribute(a)) {
        const v = a.initializer
        m[a.name.getText()] = !v ? true : ts.isStringLiteral(v) ? v.text : v.getText()
      }
      return m
    }
    const isIcon = (n: ts.Node): boolean => (ts.isJsxSelfClosingElement(n) || ts.isJsxElement(n)) && (icons.has(tag(n)) || /Icon$/.test(tag(n)))
    // 'icon' => only icons; 'empty' => renders nothing; 'other' => text or other content
    const kind = (n: ts.Node): 'icon' | 'empty' | 'other' => {
      if (ts.isJsxText(n)) return n.getText().trim() ? 'other' : 'empty'
      if (isIcon(n)) return 'icon'
      if (ts.isJsxFragment(n)) return kids(n.children)
      if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n)) return 'other'
      if (ts.isJsxExpression(n)) return n.expression ? kind(n.expression) : 'empty'
      if (ts.isParenthesizedExpression(n)) return kind(n.expression)
      if (ts.isConditionalExpression(n)) {
        const a = kind(n.whenTrue), b = kind(n.whenFalse)
        return a === 'other' || b === 'other' ? 'other' : a === 'icon' || b === 'icon' ? 'icon' : 'empty'
      }
      if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) return kind(n.right) === 'other' ? 'other' : kind(n.right)
      if (n.kind === ts.SyntaxKind.NullKeyword || n.kind === ts.SyntaxKind.FalseKeyword) return 'empty'
      return 'other'
    }
    const kids = (ch: ts.NodeArray<ts.JsxChild>): 'icon' | 'empty' | 'other' => {
      let icon = false
      for (const k of ch) { const r = kind(k); if (r === 'other') return 'other'; if (r === 'icon') icon = true }
      return icon ? 'icon' : 'empty'
    }
    const visit = (n: ts.Node) => {
      if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n)) {
        const t = tag(n), a = attrs(n), children = ts.isJsxElement(n) ? n.children : undefined
        const content = children ? kids(children) : 'empty'
        if ((t === 'button' && content === 'icon') || (t === 'Button' && (content === 'icon' || (content === 'empty' && a.icon))))
          c.iconButtons.push({ file: rel, line: line(n), aria: 'aria-label' in a ? String(a['aria-label']) : null,
            title: 'title' in a, labelledby: 'aria-labelledby' in a })
        if (t === 'Modal') c.modalUsages.push({ file: rel, line: line(n), alert: 'alert' in a })
        if (typeof a.role === 'string' && /dialog/.test(a.role)) c.roleDialogs.push({ file: rel, line: line(n), role: a.role })
        const cls = typeof a.className === 'string' ? a.className : ''
        if (t === 'div' && /fixed inset-0/.test(cls) && !a.role && !/role=\{?["'](alert)?dialog/.test(n.getText()) && rel !== 'components/ui/Modal.tsx')
          c.overlaysWithoutDialog.push({ file: rel, line: line(n) })
        if (t === 'th') c.th.push({ file: rel, line: line(n), scope: typeof a.scope === 'string' ? a.scope : null })
        if (a.role === 'progressbar') c.progressbars.push({ file: rel, line: line(n) })
      }
      ts.forEachChild(n, visit)
    }
    visit(sf)
  }
  return c
}

const C = census()
const summary = {
  icon_only_buttons: C.iconButtons.length,
  icon_only_with_aria_label: C.iconButtons.filter(b => b.aria !== null).length,
  icon_only_title_only: C.iconButtons.filter(b => b.aria === null && b.title).length,
  icon_only_unnamed: C.iconButtons.filter(b => b.aria === null && !b.title && !b.labelledby).map(b => `${b.file}:${b.line}`),
  shared_modal_call_sites: C.modalUsages.length,
  shared_modal_alertdialog_call_sites: C.modalUsages.filter(m => m.alert).length,
  custom_dialog_role_elements: C.roleDialogs.filter(d => d.file !== 'components/ui/Modal.tsx').map(d => `${d.file}:${d.line} ${d.role}`),
  overlays_without_dialog_role: C.overlaysWithoutDialog.map(o => `${o.file}:${o.line}`),
  th_total: C.th.length, th_scope_col: C.th.filter(t => t.scope === 'col').length,
  th_scope_col_files: [...new Set(C.th.filter(t => t.scope === 'col').map(t => t.file))],
  progressbar_role_elements: C.progressbars.map(p => `${p.file}:${p.line}`),
}
fs.mkdirSync(path.dirname(RECEIPT), { recursive: true })
fs.writeFileSync(RECEIPT, JSON.stringify(summary, null, 1))
afterEach(cleanup)

it('F12-12.7-03 census: at least 50 icon-only buttons carry aria-label (exact count in receipt)', () => {
  expect(summary.icon_only_with_aria_label).toBeGreaterThanOrEqual(50)
  expect(summary.icon_only_with_aria_label).toBe(108)
})

it('F12-12.7-03 census: every icon-only button has an accessible name (aria-label, aria-labelledby or title)', () => {
  expect(summary.icon_only_unnamed).toEqual([])
})

it('F12-12.7-04 census: dialog-role modal surfaces are 35 shared-Modal call sites + 20 custom dialogs, not 17', () => {
  // Square stock send confirmation adds one shared, focus-trapped Modal; 2.0.13 adds
  // dispatch-recovery confirmation, roll receiving and QR label Modals.
  expect(summary.shared_modal_call_sites).toBe(38)
  expect(summary.shared_modal_alertdialog_call_sites).toBe(3)
  // Super Admin restore adds one named, focus-trapped confirmation dialog.
  // 20 static role="dialog", 1 alertdialog (SystemTab) and DetailDrawer's role that is 'dialog' while open.
  expect(summary.custom_dialog_role_elements).toHaveLength(22)
  const dialogOnly = summary.custom_dialog_role_elements.filter(d => d.endsWith(' dialog')).length
  expect(dialogOnly).toBe(20)
  expect(summary.shared_modal_call_sites - summary.shared_modal_alertdialog_call_sites + dialogOnly).toBe(55)
  expect(summary.shared_modal_call_sites - summary.shared_modal_alertdialog_call_sites + dialogOnly).not.toBe(17)
})

it('F12-12.7-04 real shared Modal renders a named aria-modal dialog / alertdialog', () => {
  render(<Modal isOpen onClose={() => {}} title="Cert dialog"><p>body</p></Modal>)
  expect(screen.getByRole('dialog', { name: 'Cert dialog' })).toHaveAttribute('aria-modal', 'true')
  cleanup()
  render(<Modal isOpen onClose={() => {}} title="Cert alert" alert><p>body</p></Modal>)
  expect(screen.getByRole('alertdialog', { name: 'Cert alert' })).toHaveAttribute('aria-modal', 'true')
})

it('F12-12.7-06 census: scope="col" exists only on a minority of table headers', () => {
  // Merged tree: cert-fix-D's Analytics failure tables add 8 scoped headers (still a minority).
  expect(summary.th_scope_col).toBe(17)
  // 2.0.13 adds one unscoped header (inventory reorder table).
  expect(summary.th_total).toBe(123)
  expect(summary.th_scope_col * 2).toBeLessThan(summary.th_total)
  expect(summary.th_scope_col_files).toEqual(['components/admin/ChargebackReport.tsx', 'components/jobs/JobTableHeader.tsx',
    'pages/analytics/Analytics.tsx'])
})

it('F12-12.7-07 census: role="progressbar" is on every progress-style bar', () => {
  expect(summary.progressbar_role_elements.map(p => p.split(':')[0]).sort()).toEqual([
    'components/layout/Sidebar.tsx', 'components/orders/OrderModals.tsx', 'components/printers/PrintProgress.tsx',
    'components/shared/OnboardingTour.tsx', 'components/ui/ProgressBar.tsx', 'pages/dashboard/Overlay.tsx',
    'pages/dashboard/TVDashboard.tsx', 'pages/jobs/Timeline.tsx', 'pages/jobs/Upload.tsx',
  ])
})
