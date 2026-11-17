// Axe can omit clipped content. Scan overlapping main viewports and retain each
// result, including failures found only below the fold. Never scroll the window.
export async function scanMainRegion(page, analyze) {
  const main = page.locator('main')
  const originalTop = await main.evaluate(element => element.scrollTop)
  const scans = []
  try {
    let top = 0
    while (true) {
      await main.evaluate((element, nextTop) => { element.scrollTop = nextTop }, top)
      // Wait for scroll-driven rendering and the next paint before asking axe.
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      const position = await main.evaluate(element => ({
        top: element.scrollTop, height: element.clientHeight, end: element.scrollHeight - element.clientHeight,
      }))
      scans.push({ scrollTop: position.top, result: await analyze() })
      if (position.top >= position.end) break
      const next = Math.min(position.end, position.top + Math.max(1, Math.floor(position.height * .8)))
      if (position.height <= 0 || (top > 0 && position.top < top - 1)) throw new Error('Main region did not scroll to the requested position')
      top = next
    }
    return scans
  } finally {
    await main.evaluate((element, top) => { element.scrollTop = top }, originalTop)
  }
}
