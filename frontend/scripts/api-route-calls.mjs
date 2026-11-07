// Parse production TS/JS using TypeScript, preserving all conditional URL branches.
import ts from 'typescript'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')
const calls = []
let constants, aliases
const cross = (a, b) => a.flatMap(x => b.map(y => x + y))
function values(node, seen = new Set()) {
  if (!node) return ['{param}']
  if (ts.isStringLiteralLike(node)) return [node.text]
  if (ts.isIdentifier(node)) {
    // Enumerate typed action segments (e.g. 'trim' | 'speed'), not a wildcard.
    let parent = node.parent
    while (parent) {
      if (parent.parameters) {
        const parameter = parent.parameters.find(p => p.name.getText() === node.text)
        if (parameter) {
          const finite = typeValues(parameter.type)
          if (finite) return finite
          break
        }
      }
      parent = parent.parent
    }
  }
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && !seen.has(node.expression.text)) {
    const fn = constants.get(node.expression.text)
    if (fn && (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn) || ts.isFunctionDeclaration(fn))) {
      const returns = ts.isBlock(fn.body) ? fn.body.statements.filter(ts.isReturnStatement).map(s => s.expression) : [fn.body]
      if (returns.length) return [...new Set(returns.flatMap(r => values(r, new Set([...seen, node.expression.text]))))]
    }
  }
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)) return values(node.expression, seen)
  if (ts.isIdentifier(node) && constants.has(node.text) && !seen.has(node.text)) return values(constants.get(node.text), new Set([...seen, node.text]))
  if (ts.isConditionalExpression(node)) return [...new Set([...values(node.whenTrue, seen), ...values(node.whenFalse, seen)])]
  if (ts.isTemplateExpression(node)) return node.templateSpans.reduce((out, s) => cross(out, values(s.expression, seen).map(v => v + s.literal.text)), [node.head.text])
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) return cross(values(node.left, seen), values(node.right, seen))
  return ['{param}']
}
function typeValues(type) {
  if (!type) return null
  if (ts.isLiteralTypeNode(type) && ts.isStringLiteral(type.literal)) return [type.literal.text]
  if (ts.isUnionTypeNode(type)) {
    const all = type.types.map(typeValues)
    return all.every(Boolean) ? all.flat() : null
  }
  if (ts.isTypeReferenceNode(type) && aliases.has(type.typeName.getText())) return typeValues(aliases.get(type.typeName.getText()))
  return null
}
function scan(file, source) {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
  constants = new Map()
  aliases = new Map()
  function collect(node) {
    if (ts.isTypeAliasDeclaration(node)) aliases.set(node.name.text, node.type)
    if (ts.isFunctionDeclaration(node) && node.name) constants.set(node.name.text, node)
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      // Ambiguous/shadowed names require an explicit reviewed dynamic entry.
      constants.set(node.name.text, constants.has(node.name.text) ? null : node.initializer)
    }
    ts.forEachChild(node, collect)
  }
  collect(tree)
  constants = new Map([...constants].filter(([, value]) => value !== null))
  function visit(node) {
    if (ts.isCallExpression(node) && ['fetch', 'fetchAPI'].includes(node.expression.getText(tree)) && node.arguments[0]) {
      const fn = node.expression.getText(tree), arg = node.arguments[0]
      let methods = ['GET']
      let options = node.arguments[1]
      if (options && ts.isIdentifier(options) && constants.has(options.text)) options = constants.get(options.text)
      if (options && ts.isObjectLiteralExpression(options)) {
        const prop = options.properties.find(p => p.name?.getText(tree).replace(/['"]/g, '') === 'method')
        if (prop) methods = values(prop.initializer)
        else if (options.properties.some(p => ts.isSpreadAssignment(p))) methods = ['{param}']
      } else if (options && ts.isCallExpression(options) && options.expression.getText(tree) === 'body' && file === 'api/educationClasses.ts') {
        // This local helper accepts method as its second argument.
        methods = values(options.arguments[1])
      } else if (options) methods = ['{param}']
      calls.push({ file, line: tree.getLineAndCharacterOfPosition(node.getStart()).line + 1, fn,
        expression: arg.getText(tree), paths: values(arg).map(v => (fn === 'fetchAPI' ? '/api' : '') + v),
        methods, options: node.arguments[1]?.getText(tree) ?? '' })
    }
    ts.forEachChild(node, visit)
  }
  visit(tree)
}
function walk(dir) {
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const file = path.join(dir, entry.name)
    if (entry.isDirectory()) { if (entry.name !== 'test' && entry.name !== '__tests__') walk(file) }
    else if (/\.[jt]sx?$/.test(file) && !/\.(test|spec)\.[jt]sx?$/.test(file)) scan(path.relative(root, file), fs.readFileSync(file, 'utf8'))
  }
}
if (process.argv.includes('--stdin')) scan('fixture.tsx', fs.readFileSync(0, 'utf8'))
else walk(root)
console.log(JSON.stringify(calls))
