import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, rmSync } from 'node:fs'
import { dirname, relative, join } from 'node:path'

const check = process.argv.includes('--check')
const stale = []
const outputs = new Set()
const managedDirectories = []
export function manageDirectory(path) { managedDirectories.push(path) }
let count = 0
export function emit(path, content) {
  count++
  outputs.add(path)
  if (check) {
    if (!existsSync(path) || readFileSync(path, 'utf8') !== content) stale.push(relative(process.cwd(), path))
  } else {
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, content)
  }
}
export function finish() {
  // Removed/renamed color tokens must not leave obsolete native assets behind.
  for (const dir of managedDirectories) {
    if (!existsSync(dir)) continue
    for (const file of readdirSync(dir, { recursive: true, withFileTypes: true })) {
      if (!file.isFile()) continue
      const path = join(file.parentPath, file.name)
      if (outputs.has(path)) continue
      if (check) stale.push(relative(process.cwd(), path))
      else rmSync(path)
    }
  }
  if (stale.length) {
    console.error(`Stale generated files:\n${stale.join('\n')}\nRun node design/generate.mjs`)
    process.exitCode = 1
  } else console.log(`${check ? 'Checked' : 'Generated'} ${count} files (all outputs local).`)
}
