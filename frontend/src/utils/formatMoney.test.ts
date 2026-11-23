// @ts-ignore -- Node built-ins are available to Vitest.
import fs from 'node:fs'
// @ts-ignore -- Node built-ins are available to Vitest.
import path from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'
import { formatMoney } from './formatMoney'

describe('display currency', () => {
  it.each(['USD','CAD','AUD','NZD','GBP','EUR','CHF','JPY'])('labels amounts with %s', code => {
    const result = formatMoney(12.5, code)
    expect(result).toContain(code)
    expect(result).toContain(code === 'JPY' ? '13' : '12.50')
    expect(result).not.toContain('$')
  })
  it('shows a plain number when unset', () => expect(formatMoney(12.5, null)).toBe('12.50'))
})

declare const __dirname: string

function hasDollarLabel(filename: string, source: string) {
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  function visit(node: ts.Node, jsx = false): boolean {
    if (ts.isJsxText(node)) return node.text.includes('$')
    if (jsx && (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node))) return node.text.includes('$')
    return !!ts.forEachChild(node, child => visit(child, jsx || ts.isJsxExpression(node) || ts.isJsxAttribute(node)) || undefined)
  }
  return visit(file)
}

it('detects dollar labels in JSX text, strings and interpolated templates', () => {
  for (const source of ["<p>$</p>", "<p>{'$' + amount}</p>", '<p>{`Cost: ${amount}$`}</p>', '<p>{`$${amount}`}</p>']) {
    expect(hasDollarLabel('example.tsx', source)).toBe(true)
  }
  expect(hasDollarLabel('example.tsx', '<p>{`Cost: ${amount}`}</p>')).toBe(false)
})

it('keeps literal dollar labels out of rendered JSX', () => {
  const root = path.resolve(__dirname, '..')
  const violations = (fs.readdirSync(root, { recursive: true }) as string[]).filter(filename =>
    filename.endsWith('.tsx') && !filename.includes('.test.') &&
    hasDollarLabel(filename, fs.readFileSync(path.join(root, filename), 'utf8')))
  expect(violations).toEqual([])
})

it('uses invoice-style grouping and accepts numeric strings', () => {
  expect(formatMoney('1234.50', 'EUR')).toBe('EUR 1,234.50')
  expect(formatMoney(47.925, 'EUR')).toBe('EUR 47.92')
  for (const code of ['ZZZ', 'a!', 'constructor']) expect(formatMoney('12.50', code)).toBe('12.50')
  for (const value of ['', '  ', 'invalid', Infinity]) expect(formatMoney(value, 'EUR')).toBe('Unknown')
})

it('keeps rate precision even for zero-minor-unit currencies', () => {
  expect(formatMoney(0.125, 'JPY', 3)).toBe('JPY 0.125')
  expect(formatMoney(0.14567, 'EUR', 'rate')).toBe('EUR 0.14567')
  expect(formatMoney(0.145, 'JPY', 'rate')).toBe('JPY 0.145')
})
