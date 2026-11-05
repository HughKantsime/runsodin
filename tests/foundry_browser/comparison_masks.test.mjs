import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { comparisonMaskDefinitions, measureComparisonMasks, comparisonMaskRectangles } from './comparison_masks.mjs'
const require = createRequire(new URL('../../frontend/package.json', import.meta.url))
const { JSDOM } = require('jsdom')
const measured = rectangles => [{ ...comparisonMaskDefinitions[0], rectangles }]
test('only the explicit bell count selector is measured, with outward-rounded viewport pixels', () => {
  const dom = new JSDOM(`<button aria-label="Notifications, 2 unread"><svg></svg><span aria-hidden="true" class="rounded-full">2</span></button><span class="rounded-full">Other badge</span>`, { runScripts: 'outside-only' })
  const w = dom.window
  w.innerWidth = 390; w.innerHeight = 900
  w.HTMLElement.prototype.checkVisibility = () => true
  w.HTMLElement.prototype.getBoundingClientRect = () => ({ left: 305.5, top: 14, right: 323.5, bottom: 32 })
  w.definitions = comparisonMaskDefinitions
  const masks = JSON.parse(JSON.stringify(w.eval(`(${measureComparisonMasks.toString()})(definitions)`)))
  assert.deepEqual(masks, measured([{ x: 305, y: 14, width: 19, height: 18 }]))
  assert.deepEqual(comparisonMaskRectangles(masks, masks), masks[0].rectangles)
  dom.window.close()
})
test('absent badges produce an explicit empty mask list, never a guessed rectangle', () => {
  assert.deepEqual(comparisonMaskRectangles(measured([]), measured([])), [])
})
test('moved, resized, missing badges and unmeasured old baselines fail closed', () => {
  const actual = measured([{ x: 305, y: 14, width: 19, height: 18 }])
  for (const base of [undefined, [], measured([]), measured([{ x: 306, y: 14, width: 19, height: 18 }]), measured([{ x: 305, y: 14, width: 20, height: 18 }])]) {
    assert.throws(() => comparisonMaskRectangles(actual, base), /mask/i)
  }
})
test('unknown selectors and invalid rectangles cannot widen the allowlist', () => {
  for (const masks of [[{ ...comparisonMaskDefinitions[0], selector: 'body', rectangles: [] }], measured([{ x: -1, y: 0, width: 5, height: 5 }])]) {
    assert.throws(() => comparisonMaskRectangles(masks, masks), /Invalid/)
  }
})
