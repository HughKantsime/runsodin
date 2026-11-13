import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

// "\u2014" typed as JSX text is not an escape: the browser shows the six characters.
// It is only an escape inside a string ('…', "…", `…`). Found on the setup wizard in 2.1.1.
function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sources(path)
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [path] : []
  })
}

export function literalEscapes(text: string): number[] {
  const lines: number[] = []
  text.split('\n').forEach((line, index) => {
    for (const match of line.matchAll(/\\u[0-9A-Fa-f]{4}/g)) {
      const before = line.slice(0, match.index)
      const open = (quote: string) => (before.match(new RegExp(`(?<!\\\\)${quote}`, 'g')) || []).length % 2 === 1
      // A regex literal such as /\u00A0/ is also a real escape.
      const inRegex = /\/[^/\n]*$/.test(before) && /^[^/\n]*\/[gimsuy]*[),.;\s]/.test(line.slice(match.index))
      if (!open("'") && !open('"') && !open('`') && !inRegex) lines.push(index + 1)
    }
  })
  return lines
}

describe('JSX text never contains a typed-out \\uXXXX escape', () => {
  it('recognises the mistake and the correct forms', () => {
    expect(literalEscapes('<p>Skip \\u2014 later</p>')).toEqual([1])
    expect(literalEscapes("<p>{'\\u2014'} later</p>")).toEqual([])
    expect(literalEscapes('const dash = "\\u2014"')).toEqual([])
    expect(literalEscapes('value.replace(/\\u00A0/g, " ")')).toEqual([])
  })

  it('holds for every component and page', () => {
    const found = sources(join(__dirname, '..')).flatMap((path) =>
      literalEscapes(readFileSync(path, 'utf-8')).map((line) => `${path.split('/src/')[1]}:${line}`))
    expect(found).toEqual([])
  })
})
