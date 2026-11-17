// @vitest-environment node
import { expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import postcss from 'postcss'
import * as csstree from 'css-tree'

// Existing unsuffixed emerald and interaction-only colors are outside this
// opacity regression. Keep this finite debt list explicit: no opacity class or
// new shade/variant is exempt. These still need a separate semantic review.
const allowed = new Set([
  'text-emerald-200', // Alerts: recovered status.
  'text-emerald-300', // Setup: successful connection/configuration.
  'text-emerald-400', // Setup, Utilization, Products: success and numeric summaries.
  'text-emerald-500', // PrintNext: in-stock status.
  'file:text-amber-200', // Setup file-upload pseudo-element.
  'hover:text-amber-100', 'hover:text-amber-300', 'hover:text-amber-400',
  'hover:text-yellow-300', 'hover:text-yellow-400', // Existing hover accents.
  'hover:text-red-300', 'hover:text-red-400', // Existing destructive hover actions.
])

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const name = `${dir}/${entry.name}`
    return entry.isDirectory() ? sourceFiles(name)
      : /\.[jt]sx?$/.test(name) && !/\.(test|spec)\./.test(name) ? [name] : []
  })
}
function mappedClasses(property = 'color') {
  const mapped = new Set<string>()
  postcss.parse(readFileSync('src/fjord.css', 'utf8')).walkRules(rule => {
    if (!rule.nodes.some(n => n.type === 'decl' && n.prop === property && n.value.startsWith('var('))) return
    const selector = csstree.parse(rule.selector, { context: 'selectorList' })
    csstree.walk(selector, node => {
      if (node.type === 'ClassSelector') mapped.add(csstree.ident.decode(node.name))
      if (node.type === 'AttributeSelector' && node.name.name === 'class' && node.matcher === '~=' && node.value?.type === 'String') mapped.add(node.value.value)
    })
  })
  return mapped
}
it('requires a Fjord text mapping or explicit exception for every status utility, including opacity and variants', () => {
  const mapped = mappedClasses()
  const missing: string[] = []
  for (const file of sourceFiles('src')) {
    const source = readFileSync(file, 'utf8')
    for (const match of source.matchAll(/(?<![\w:-])(?:[\w-]+:)*text-(?:orange|amber|yellow|emerald|red)-\d+(?:\/(?:\d+|\[[^\]]+\]))?(?![\w/-])/g)) {
      if (!mapped.has(match[0]) && !allowed.has(match[0])) missing.push(`${file}:${source.slice(0, match.index).split('\n').length}: ${match[0]}`)
    }
  }
  expect(missing).toEqual([])
})

it('maps warning background tints, including hover states, to their tested pair', () => {
  const mapped = mappedClasses('background-color')
  const exceptions = new Set(['file:bg-amber-500/20', 'hover:file:bg-amber-500/30', 'bg-amber-700/50']) // Upload pseudo-element and timeline setup bar, not warning text.
  const missing: string[] = []
  for (const file of sourceFiles('src')) {
    const source = readFileSync(file, 'utf8')
    for (const match of source.matchAll(/(?<![\w:-])(?:[\w-]+:)*bg-(?:orange|amber|yellow)-\d+\/\d+(?![\w/-])/g)) {
      if (!mapped.has(match[0]) && !exceptions.has(match[0])) missing.push(`${file}: ${match[0]}`)
    }
  }
  expect(missing).toEqual([])
})
