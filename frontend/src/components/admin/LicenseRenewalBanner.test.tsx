import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import LicenseRenewalBanner, { formatLicenseDate, renewalBannerState, type BannerLicense } from './LicenseRenewalBanner'

const NOW = Date.parse('2026-11-11T12:00:00Z')
const DAY = 86_400_000
const license = (over: Partial<BannerLicense> = {}): BannerLicense => ({
  valid: true, trial: false, tier_name: 'Pro', expires_at: '2026-11-18T00:00:00Z', managed_externally: false, last_refresh: null, ...over,
})
const last = (status: 'renewed' | 'not_renewed_yet' | 'canceled' | 'not_entitled') =>
  ({ status, at: '2026-11-10T09:00:00Z', expires_at: '2026-11-18T00:00:00Z' })
afterEach(cleanup)

test('the renewal window opens exactly 7 days before the end', () => {
  expect(renewalBannerState(license({ expires_at: new Date(NOW + 7 * DAY).toISOString() }), NOW)).toBe('due')
  expect(renewalBannerState(license({ expires_at: new Date(NOW + 7 * DAY + 1).toISOString() }), NOW)).toBeNull()
  expect(renewalBannerState(license({ expires_at: '2026-11-17' }), NOW)).toBe('due')
})

test.each([
  ['a trial', license({ trial: true })],
  ['an externally managed license', license({ managed_externally: true })],
  ['no valid license', license({ valid: false })],
  ['no end date', license({ expires_at: null })],
  ['nothing loaded', null],
])('no banner for %s', (_name, value) => {
  expect(renewalBannerState(value, NOW)).toBeNull()
})

test('a renewed answer alone shows nothing; the window rule still applies', () => {
  expect(renewalBannerState(license({ expires_at: '2026-12-30T00:00:00Z', last_refresh: last('renewed') }), NOW)).toBeNull()
  expect(renewalBannerState(license({ last_refresh: last('renewed') }), NOW)).toBe('due')
})

test('the last answer is shown whatever the end date', () => {
  for (const status of ['not_renewed_yet', 'canceled', 'not_entitled'] as const) {
    expect(renewalBannerState(license({ expires_at: '2027-06-01T00:00:00Z', last_refresh: last(status) }), NOW)).toBe(status)
  }
  expect(renewalBannerState(license({ valid: false, last_refresh: last('not_entitled') }), NOW)).toBe('not_entitled')
})

test('dates are shown in UTC', () => {
  expect(formatLicenseDate('2026-11-18T00:00:00Z')).toBe('November 18, 2026')
  expect(formatLicenseDate('2026-11-17')).toBe('November 17, 2026')
  expect(formatLicenseDate(null)).toBe('')
})

test('Renewal due says nothing about the subscription and holds the actions', () => {
  render(<LicenseRenewalBanner license={license()} nowMs={NOW} actions={<button>Refresh license</button>} />)
  const banner = screen.getByTestId('license-renewal-banner')
  expect(banner).toHaveAttribute('data-state', 'due')
  expect(banner).toHaveTextContent('Renewal due')
  expect(banner).toHaveTextContent('Your Pro license file ends on November 18, 2026. If your subscription has renewed, click Refresh license to load the new file.')
  expect(banner).not.toHaveTextContent(/cancel|declined|renews on/i)
  expect(screen.getByRole('button', { name: 'Refresh license' })).toBeInTheDocument()
})

test('not renewed yet names the refresh date and links to Manage subscription', () => {
  render(<LicenseRenewalBanner license={license({ last_refresh: last('not_renewed_yet') })} nowMs={NOW} />)
  const banner = screen.getByTestId('license-renewal-banner')
  expect(banner).toHaveTextContent('As of your last refresh on November 10, 2026, the license portal shows that your subscription has not renewed.')
  expect(screen.getByRole('link', { name: 'Manage subscription' })).toHaveAttribute('href', 'https://runsodin.com/manage-subscription')
})

test('canceled: Pro features stop on the end date, and Refresh stays available', () => {
  render(<LicenseRenewalBanner license={license({ last_refresh: last('canceled') })} nowMs={NOW} actions={<button>Refresh license</button>} />)
  const banner = screen.getByTestId('license-renewal-banner')
  expect(banner).toHaveTextContent('the license portal reports that your subscription was cancelled.')
  expect(banner).toHaveTextContent('Pro features stop on November 18, 2026.')
  expect(screen.getByRole('button', { name: 'Refresh license' })).toBeInTheDocument()
})

test('canceled after the end date reads in the past tense', () => {
  render(<LicenseRenewalBanner license={license({ valid: false, last_refresh: last('canceled') })} nowMs={Date.parse('2026-11-20T00:00:00Z')} />)
  expect(screen.getByTestId('license-renewal-banner')).toHaveTextContent('Pro features stopped on November 18, 2026.')
})

test('not entitled: Pro has ended, with a support link and no claim about the reason', () => {
  render(<LicenseRenewalBanner license={license({ valid: false, last_refresh: last('not_entitled') })} nowMs={NOW} actions={<button>Refresh license</button>} />)
  const banner = screen.getByTestId('license-renewal-banner')
  expect(banner).toHaveAttribute('data-state', 'not_entitled')
  expect(banner).toHaveTextContent('Pro has ended')
  expect(banner).toHaveTextContent('the license portal reports that this license is no longer active. Nothing is deleted.')
  expect(banner).not.toHaveTextContent(/refund|chargeback/i)
  expect(screen.getByRole('link', { name: 'support@runsodin.com' })).toHaveAttribute('href', 'mailto:support@runsodin.com')
  expect(screen.getByRole('button', { name: 'Refresh license' })).toBeInTheDocument()
})

test('nothing renders when no banner applies', () => {
  const { container } = render(<LicenseRenewalBanner license={license({ expires_at: '2027-06-01T00:00:00Z' })} nowMs={NOW} />)
  expect(container).toBeEmptyDOMElement()
})
