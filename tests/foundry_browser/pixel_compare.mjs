/** Pure RGBA comparison, also injected into the browser's PNG decoding page. */
export function comparePixels(actual, baseline, tolerance = 10, masks = []) {
  const width = Math.max(actual.width, baseline.width)
  const height = Math.max(actual.height, baseline.height)
  const diff = new Uint8ClampedArray(width * height * 4)
  let changedPixels = 0, minX = width, minY = height, maxX = -1, maxY = -1
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const a = (y * actual.width + x) * 4, b = (y * baseline.width + x) * 4
    const output = (y * width + x) * 4
    const outsideA = x >= actual.width || y >= actual.height
    const outsideB = x >= baseline.width || y >= baseline.height
    const masked = masks.some(r => x >= r.x && y >= r.y && x < r.x + r.width && y < r.y + r.height)
    const changed = outsideA !== outsideB || (!masked && !outsideA && !outsideB && [0, 1, 2, 3].some(c => Math.abs(actual.data[a + c] - baseline.data[b + c]) > tolerance))
    if (changed) {
      changedPixels++; minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y)
      diff.set([255, 0, 80, 255], output)
    } else {
      const gray = masked || outsideA ? 0 : Math.round((actual.data[a] + actual.data[a + 1] + actual.data[a + 2]) / 6)
      diff.set([gray, gray, gray, 255], output)
    }
  }
  return { width, height, diff, changedPixels, boundingBox: changedPixels ? { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 } : null }
}

/** Bounding-box containment proves that every changed pixel is inside the
 * calibrated noise area. Dimensions must still match; volatility isn't a
 * blanket exemption for a filename or a relocated/resized capture. */
export function volatilePass(comparison, reference) {
  const box = comparison.boundingBox, base = reference?.boundingBox
  if (!comparison.changedPixels || !reference?.changedPixels || !box || !base) return false
  if (comparison.changedPixels > reference.changedPixels * 2) return false
  if (![box.x, box.y, box.width, box.height, base.x, base.y, base.width, base.height, reference.changedPixels].every(Number.isFinite)
    || reference.changedPixels < 1 || base.width < 1 || base.height < 1) return false
  if (JSON.stringify(comparison.actualSize) !== JSON.stringify(comparison.baselineSize)) return false
  if (!Array.isArray(reference.actualSize) || JSON.stringify(reference.actualSize) !== JSON.stringify(reference.baselineSize)
    || JSON.stringify(comparison.actualSize) !== JSON.stringify(reference.actualSize)) return false
  return box.x >= base.x - 4 && box.y >= base.y - 4
    && box.x + box.width <= base.x + base.width + 4
    && box.y + box.height <= base.y + base.height + 4
}
