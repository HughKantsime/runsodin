/** Explicit review-approved exclusions only. No diff-derived/automatic masks.
 * This selector also exists in the untouched pre-Foundry frontend. */
export const comparisonMaskDefinitions = [
  { id: 'alert-bell-count', selector: 'button[aria-label^="Notifications,"] > span[aria-hidden="true"].rounded-full' },
]

/** Evaluate in the capture page. Screenshots are viewport-sized at DPR 1. */
export function measureComparisonMasks(definitions) {
  return definitions.map(({ id, selector }) => ({
    id, selector,
    rectangles: [...document.querySelectorAll(selector)].filter(el => el.checkVisibility({ visibilityProperty: true, opacityProperty: true }))
      .map(el => {
        const r = el.getBoundingClientRect()
        const x = Math.max(0, Math.floor(r.left)), y = Math.max(0, Math.floor(r.top))
        return { x, y, width: Math.min(innerWidth, Math.ceil(r.right)) - x, height: Math.min(innerHeight, Math.ceil(r.bottom)) - y }
      }).filter(r => r.width > 0 && r.height > 0),
  }))
}

/** Do not let exclusions conceal a missing, moved or resized badge. Old reports
 * without selector measurements must be recaptured, never guessed from pixels. */
export function comparisonMaskRectangles(actual, baseline) {
  for (const masks of [actual, baseline]) {
    if (!Array.isArray(masks) || masks.length !== comparisonMaskDefinitions.length) throw new Error('Missing comparison mask measurements; recapture with the current harness')
    masks.forEach((mask, i) => {
      const definition = comparisonMaskDefinitions[i]
      if (mask.id !== definition.id || mask.selector !== definition.selector || !Array.isArray(mask.rectangles)
        || mask.rectangles.some(r => ![r.x, r.y, r.width, r.height].every(Number.isInteger) || r.x < 0 || r.y < 0 || r.width <= 0 || r.height <= 0)) throw new Error('Invalid comparison mask measurements')
    })
  }
  if (JSON.stringify(actual) !== JSON.stringify(baseline)) throw new Error('Comparison mask geometry differs from baseline')
  return actual.flatMap(mask => mask.rectangles)
}
