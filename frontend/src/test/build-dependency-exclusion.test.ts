import { expect, it, vi } from 'vitest'
import { buildDependencyExclusion } from '../../vite.config.js'

it.each(['braces', 'chokidar', 'fast-glob', 'micromatch', 'tailwindcss'])('rejects %s in browser modules', name => {
  const context = { getModuleIds: () => [`/build/node_modules/${name}/index.js`],
    error: (message: string) => { throw new Error(message) }, emitFile: vi.fn() }
  expect(() => buildDependencyExclusion().generateBundle.call(context)).toThrow('Build-only dependency')
  expect(context.emitFile).not.toHaveBeenCalled()
})
it('emits build evidence for application modules only', () => {
  const context = { getModuleIds: () => ['/src/main.tsx'], error: vi.fn(), emitFile: vi.fn() }
  buildDependencyExclusion().generateBundle.call(context)
  expect(context.emitFile).toHaveBeenCalledWith(expect.objectContaining({
    source: JSON.stringify({ status: 'pass', checked_modules: 1, forbidden_modules: 0 }),
  }))
})
