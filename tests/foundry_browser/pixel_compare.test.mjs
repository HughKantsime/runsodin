import { test } from 'node:test'
import assert from 'node:assert/strict'
import { comparePixels } from './pixel_compare.mjs'
const raster = (width, height, data) => ({ width, height, data: new Uint8ClampedArray(data) })
test('all channel differences through 10 are unchanged', () => {
  const result = comparePixels(raster(1, 1, [10, 90, 110, 245]), raster(1, 1, [0, 100, 100, 255]))
  assert.equal(result.changedPixels, 0)
  assert.equal(result.boundingBox, null)
})
test('a single channel difference of 11 fails and is marked in the diff', () => {
  const result = comparePixels(raster(2, 1, [0, 0, 0, 255, 0, 11, 0, 255]), raster(2, 1, [0, 0, 0, 255, 0, 0, 0, 255]))
  assert.equal(result.changedPixels, 1)
  assert.deepEqual(result.boundingBox, { x: 1, y: 0, width: 1, height: 1 })
  assert.deepEqual([...result.diff.slice(4)], [255, 0, 80, 255])
})
test('dimension changes fail and identify the extra strip', () => {
  const result = comparePixels(raster(2, 1, [0, 0, 0, 255, 0, 0, 0, 255]), raster(1, 1, [0, 0, 0, 255]))
  assert.equal(result.changedPixels, 1)
  assert.deepEqual(result.boundingBox, { x: 1, y: 0, width: 1, height: 1 })
})
test('explicit mask ignores only its rectangle, including neither adjacent edge', () => {
  const base = raster(3, 3, new Array(36).fill(0)), actual = raster(3, 3, new Array(36).fill(255))
  const result = comparePixels(actual, base, 10, [{ x: 1, y: 1, width: 1, height: 1 }])
  assert.equal(result.changedPixels, 8)
  assert.deepEqual([...result.diff.slice(16, 20)], [0, 0, 0, 255])
  assert.deepEqual(result.boundingBox, { x: 0, y: 0, width: 3, height: 3 })
})
test('a mask cannot hide a dimension change', () => {
  const result = comparePixels(raster(2, 1, new Array(8).fill(0)), raster(1, 1, new Array(4).fill(0)), 10, [{ x: 0, y: 0, width: 2, height: 1 }])
  assert.equal(result.changedPixels, 1)
})
test('bounding box contains all changed pixels, including alpha changes', () => {
  const base = raster(3, 2, new Array(24).fill(100)), actual = raster(3, 2, base.data)
  actual.data[4] = 111; actual.data[23] = 89
  const result = comparePixels(actual, base)
  assert.equal(result.changedPixels, 2)
  assert.deepEqual(result.boundingBox, { x: 1, y: 0, width: 2, height: 2 })
})

const { volatilePass } = await import('./pixel_compare.mjs')
const noise = { changedPixels: 10, boundingBox: { x: 100, y: 200, width: 6, height: 8 }, actualSize: [390, 900], baselineSize: [390, 900] }
test('volatile passes include the exact padded boundary and twice the pixel count', () => {
  assert.equal(volatilePass({ ...noise, changedPixels: 20, boundingBox: { x: 96, y: 196, width: 14, height: 16 } }, noise), true)
})
test('volatile fails when one changed pixel lies outside any padded edge', () => {
  for (const boundingBox of [{ x: 95, y: 200, width: 1, height: 1 }, { x: 100, y: 195, width: 1, height: 1 }, { x: 110, y: 200, width: 1, height: 1 }, { x: 100, y: 212, width: 1, height: 1 }]) {
    assert.equal(volatilePass({ ...noise, changedPixels: 1, boundingBox }, noise), false)
  }
})
test('volatile fails above twice the count even within the allowed box', () => {
  assert.equal(volatilePass({ ...noise, changedPixels: 21 }, noise), false)
})
test('unlisted captures and invalid noise records keep the strict comparison', () => {
  for (const reference of [undefined, {}, { ...noise, changedPixels: 0 }, { ...noise, boundingBox: null }, { ...noise, changedPixels: NaN }]) {
    assert.equal(volatilePass(noise, reference), false)
  }
})
test('volatility cannot hide dimension changes or incompatible calibration sizes', () => {
  assert.equal(volatilePass({ ...noise, actualSize: [391, 900] }, noise), false)
  assert.equal(volatilePass(noise, { ...noise, actualSize: [1440, 900], baselineSize: [1440, 900] }), false)
})
test('unchanged captures are regular passes, not volatile passes', () => {
  assert.equal(volatilePass({ ...noise, changedPixels: 0, boundingBox: null }, noise), false)
})
