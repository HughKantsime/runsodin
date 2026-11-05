// @vitest-environment node
import { afterAll, expect, it } from 'vitest'
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { foundryPairings } from '../../../design/contrast.mjs'

const root = resolve('..')
const scratch = mkdtempSync(join(tmpdir(), 'odin-foundry-generation-'))
cpSync(join(root, 'design'), join(scratch, 'design'), { recursive: true })
cpSync(join(root, 'frontend/src/design-tokens.css'), join(scratch, 'classic-before.css'))
const generate = (...args: string[]) => spawnSync(process.execPath, ['design/generate.mjs', ...args], { cwd: scratch, encoding: 'utf8' })
afterAll(() => rmSync(scratch, { recursive: true, force: true }))

it('reproduces Classic byte for byte and checks all current generated outputs without writing', () => {
  expect(generate('--local-only').status).toBe(0)
  expect(readFileSync(join(scratch, 'frontend/src/design-tokens.css'))).toEqual(readFileSync(join(scratch, 'classic-before.css')))
  expect(generate('--check').status).toBe(0)
})
it('fails on every stale generated file and missing native assets', () => {
  generate()
  const native = readdirSync(join(scratch, 'design/generated/native'), { recursive: true }).filter(p => String(p).endsWith('.json') || String(p).endsWith('.swift'))
  const files = ['frontend/src/design-tokens.css', 'frontend/src/foundry-tokens.css', ...native.map(p => `design/generated/native/${p}`)]
  expect(files).toHaveLength(31)
  for (const file of files) {
    const path = join(scratch, file), before = readFileSync(path)
    writeFileSync(path, 'stale')
    const check = generate('--check')
    expect(check.status, file).toBe(1)
    expect(check.stderr).toContain(file)
    expect(readFileSync(path, 'utf8')).toBe('stale') // Check is read-only.
    writeFileSync(path, before)
  }
  rmSync(join(scratch, String(files.at(-1))))
  expect(generate('--check').status).toBe(1)
})
it('detects obsolete native outputs left by a removed token', () => {
  generate()
  writeFileSync(join(scratch, 'design/generated/native/obsolete.json'), '{}')
  expect(generate('--check').status).toBe(1)
  expect(generate().status).toBe(0)
  expect(generate('--check').status).toBe(0)
})
it('every Foundry text pairing meets WCAG AA for normal text', () => {
  const pairs = foundryPairings()
  expect(pairs).toHaveLength(55)
  for (const pair of pairs) expect(pair.ratio, pair.name).toBeGreaterThanOrEqual(4.5)
})
