import { useBranding } from '../../BrandingContext'

/** The stock product name. A white-labelled install (its own name or its own logo) never gets the ODIN mark. */
export const STOCK_APP_NAME = 'O.D.I.N.'

export function showsOdinMark(branding: { app_name?: string | null; logo_url?: string | null }): boolean {
  return !branding.logo_url && branding.app_name === STOCK_APP_NAME
}

/**
 * The ODIN eye, drawn in the install's primary colour. The shape is /odin-mark.svg used as a
 * mask, so it follows the theme and never needs a second file per colour. Decorative: the
 * product name is always written next to it.
 */
export default function OdinMark({ className = 'h-8 w-8' }: { className?: string }) {
  const branding = useBranding()
  if (!branding.brandingResolved || !showsOdinMark(branding)) return null
  const mask = 'url(/odin-mark.svg) center / contain no-repeat'
  return (
    <span
      data-testid="odin-mark"
      aria-hidden="true"
      className={`inline-block shrink-0 ${className}`}
      style={{ backgroundColor: 'var(--brand-primary)', WebkitMask: mask, mask }}
    />
  )
}
