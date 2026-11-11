import type { ReactNode } from 'react'
import { AlertTriangle, Clock } from 'lucide-react'
import { licenseExpiryMillis } from '../../LicenseContext'
import type { LastRefresh } from '../../api/licenseRefresh'

/** The banner starts this many days before the license file ends. */
export const RENEWAL_WINDOW_DAYS = 7
export const MANAGE_SUBSCRIPTION_URL = 'https://runsodin.com/manage-subscription'
export const SUPPORT_EMAIL = 'support@runsodin.com'
const DAY = 86_400_000

export interface BannerLicense {
  valid: boolean;
  trial?: boolean;
  tier_name?: string;
  expires_at: string | null;
  managed_externally?: boolean;
  last_refresh?: LastRefresh | null;
}

export type BannerState = 'due' | 'not_renewed_yet' | 'canceled' | 'not_entitled'

/** "November 18, 2026" in UTC (licenses end on UTC dates). Empty for no value. */
export function formatLicenseDate(value: string | null | undefined): string {
  if (!value) return ''
  const ms = /^\d{4}-\d{2}-\d{2}$/.test(value) ? Date.parse(value + 'T00:00:00Z') : licenseExpiryMillis(value)
  if (!Number.isFinite(ms)) return value
  return new Date(ms).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' })
}

/**
 * Which banner applies. After a refresh the portal's answer is shown (until the
 * next refresh); before any refresh ODIN only knows the file's end date, so it
 * can only say "Renewal due", from 7 days before that date. A renewed answer
 * shows nothing by itself (the refresh message does); the window rule applies.
 */
export function renewalBannerState(license: BannerLicense | null | undefined, nowMs: number): BannerState | null {
  if (!license || license.managed_externally || license.trial) return null
  const last = license.last_refresh
  if (last && last.status !== 'renewed') return last.status
  if (!license.valid || !license.expires_at) return null
  const end = licenseExpiryMillis(license.expires_at)
  if (!Number.isFinite(end)) return null
  return end - nowMs <= RENEWAL_WINDOW_DAYS * DAY ? 'due' : null
}

interface Props {
  license: BannerLicense | null | undefined;
  nowMs?: number;
  /** The one "Refresh license" button, placed inside the banner by the caller. */
  actions?: ReactNode;
}

export default function LicenseRenewalBanner({ license, nowMs = Date.now(), actions }: Props) {
  const state = renewalBannerState(license, nowMs)
  if (!license || !state) return null
  const tier = license.tier_name || 'Pro'
  const asOf = formatLicenseDate(license.last_refresh?.at)
  const manageLink = (
    <a href={MANAGE_SUBSCRIPTION_URL} target="_blank" rel="noopener noreferrer" className="text-xs font-medium underline">
      Manage subscription
    </a>
  )

  let title: string
  let body: ReactNode
  let extra: ReactNode = null
  if (state === 'due') {
    title = 'Renewal due'
    body = `Your ${tier} license file ends on ${formatLicenseDate(license.expires_at)}. If your subscription has renewed, click Refresh license to load the new file. This contacts the O.D.I.N. license portal once; offline servers can use the offline activation page instead.`
  } else if (state === 'not_renewed_yet') {
    title = 'Not renewed yet'
    body = `As of your last refresh on ${asOf}, the license portal shows that your subscription has not renewed. If a payment was declined or is unpaid, update your card under Manage subscription, then refresh again.`
    extra = manageLink
  } else if (state === 'canceled') {
    const endValue = license.last_refresh?.expires_at ?? license.expires_at
    const endMs = endValue ? (/^\d{4}-\d{2}-\d{2}$/.test(endValue) ? Date.parse(endValue + 'T00:00:00Z') + DAY : licenseExpiryMillis(endValue)) : NaN
    const stop = Number.isFinite(endMs) && endMs <= nowMs ? 'stopped' : 'stop'
    const stopSentence = endValue ? ` ${tier} features ${stop} on ${formatLicenseDate(endValue)}.` : ''
    title = 'Subscription cancelled'
    body = `As of your last refresh on ${asOf}, the license portal reports that your subscription was cancelled.${stopSentence} Nothing is deleted. You can resume your subscription under Manage subscription, then click Refresh license.`
    extra = manageLink
  } else {
    title = `${tier} has ended`
    body = (
      <>
        As of your last refresh on {asOf}, the license portal reports that this license is no longer active. Nothing is deleted.
        If you think this is a mistake, contact{' '}
        <a href={`mailto:${SUPPORT_EMAIL}`} className="font-medium underline">{SUPPORT_EMAIL}</a>.
      </>
    )
  }

  const ended = state === 'not_entitled'
  const Icon = ended ? AlertTriangle : Clock
  return (
    <div
      role="status"
      data-testid="license-renewal-banner"
      data-state={state}
      className={`mb-4 flex items-start gap-3 rounded-md border p-3 ${ended ? 'bg-red-500/10 border-red-500/30' : 'bg-amber-500/10 border-amber-500/30'}`}
    >
      <Icon size={18} className={`${ended ? 'text-red-400' : 'text-amber-400'} shrink-0 mt-0.5`} />
      <div className="min-w-0 flex-1">
        <p className={`text-sm font-medium ${ended ? 'text-red-200' : 'text-amber-200'}`}>{title}</p>
        <p className={`text-xs mt-0.5 ${ended ? 'text-red-300' : 'text-amber-300'}`}>{body}</p>
        {(actions || extra) && (
          <div className="mt-2 flex flex-wrap items-center gap-3 text-amber-200">
            {actions}
            {extra}
          </div>
        )}
      </div>
    </div>
  )
}
