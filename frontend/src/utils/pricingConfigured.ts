/**
 * Whether costs and prices may be shown: only once the pricing settings have loaded and say
 * (or, on an older server, do not deny) that someone saved them. While the request is pending
 * or after it failed there is no answer yet, so nothing is shown.
 */
export function pricingIsConfigured(settings: unknown): boolean {
  if (settings === undefined || settings === null || typeof settings !== 'object') return false
  return (settings as { configured?: boolean }).configured !== false
}
