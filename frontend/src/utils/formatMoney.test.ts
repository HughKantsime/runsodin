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

it('keeps literal dollar labels out of rendered JSX', () => {
  const violations: string[] = []
  const root = path.resolve('src')
  for (const filename of fs.readdirSync(root, { recursive: true }) as string[]) {
    if (!filename.endsWith('.tsx') || filename.includes('.test.')) continue
    const file = ts.createSourceFile(filename, fs.readFileSync(path.join(root, filename), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    function visit(node: ts.Node) {
      if (ts.isJsxText(node) && node.text.includes('$')) violations.push(filename)
      ts.forEachChild(node, visit)
    }
    visit(file)
  }
  expect(violations).toEqual([])
})
