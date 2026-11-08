import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import { LICENSE_REFRESH_EVENT } from './api/licenseEvents'
import { license as licenseAPI } from './api'
import type { ReactNode } from 'react'
import type { PublicLicense } from './types/user'

interface LicenseState {
  tier: string;
  /** False for no license and for an ended or expired one (tier still names the lapsed tier). */
  valid: boolean;
  licensee: string | null;
  expires: string | null;
  max_printers: PublicLicense['max_printers'];
  max_users: PublicLicense['max_users'];
  installation_id: string | null;
  features: string[];
  managed_externally: boolean;
  device_bound: boolean;
  loading: boolean;
  loadError: boolean;
}
interface LicenseContextValue extends LicenseState {
  isPro: boolean;
  isEducation: boolean;
  isEnterprise: boolean;
  hasFeature(feature: string): boolean;
  isProPage(page: string): boolean;
  isProSettingsTab(tab: string): boolean;
  refresh(): Promise<void>;
  atPrinterLimit(count: number): boolean;
  maxPrinters: PublicLicense['max_printers'];
  atUserLimit(count: number): boolean;
  maxUsers: PublicLicense['max_users'];
}
const LicenseContext = createContext<LicenseContextValue>({
  tier: 'community', valid: false, licensee: null, expires: null,
  max_printers: 5, max_users: 1, installation_id: null, features: [],
  managed_externally: false, device_bound: false, loading: true, loadError: false,
  isPro: false, isEducation: false, isEnterprise: false,
  hasFeature: (_feature) => false,
  isProPage: (page) => PRO_PAGES.includes(page),
  isProSettingsTab: (tab) => PRO_SETTINGS_TABS.includes(tab),
  refresh: async () => {},
  atPrinterLimit: (count) => count >= 5,
  maxPrinters: 5,
  atUserLimit: (count) => count >= 1,
  maxUsers: 1,
})

export function featuresFromLicense(features: unknown): Set<string> {
  return new Set(Array.isArray(features) ? features.filter((feature): feature is string => typeof feature === 'string') : [])
}

export const PRO_PAGES = ['orders', 'products', 'analytics', 'maintenance', 'permissions', 'branding']
export const PRO_SETTINGS_TABS = ['sso', 'webhooks', 'smtp']

export function licenseExpiryMillis(value: string): number {
  // Date-only licenses include their entire UTC day. Naive timestamps are UTC,
  // matching the backend, regardless of the browser's local timezone.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return Date.parse(value + 'T00:00:00Z') + 86400000
  return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : value + 'Z')
}

export function LicenseProvider({ children }: { children: ReactNode }) {
  const [license, setLicense] = useState<LicenseState>({
    tier: 'community', valid: false, licensee: null, expires: null, max_printers: 5, max_users: 1,
    installation_id: null, features: [], managed_externally: false, device_bound: false, loading: true, loadError: false,
  })

  const pending = useRef<Promise<void> | null>(null)
  const mounted = useRef(false)
  const fetchLicense = useCallback((): Promise<void> => {
    if (pending.current) return pending.current
    const request = async () => {
      try {
        const data = await licenseAPI.get()
        if (!mounted.current) return
        setLicense({
          tier: data.tier || 'community',
          valid: data.valid === true,
          licensee: null,
          expires: data.expires_at || null,
          max_printers: data.max_printers === undefined ? 5 : data.max_printers,
          max_users: data.max_users === undefined ? 1 : data.max_users,
          installation_id: null,
          features: Array.isArray(data.features) ? data.features : [],
          managed_externally: data.managed_externally === true,
          device_bound: data.device_bound === true,
          loading: false,
          loadError: false,
        })
      } catch {
        if (!mounted.current) return
        setLicense(previous => {
          const expired = previous.expires !== null && licenseExpiryMillis(previous.expires) <= Date.now()
          if (!expired) return { ...previous, loading: false, loadError: true }
          return {
            tier: 'community', valid: false, licensee: null, expires: null, max_printers: 5, max_users: 1,
            installation_id: null, features: [], managed_externally: false, device_bound: false, loading: false, loadError: true,
          }
        })
      }
    }
    pending.current = request().finally(() => { pending.current = null })
    return pending.current
  }, [])

  useEffect(() => {
    mounted.current = true
    const refresh = () => { void fetchLicense() }
    refresh()
    window.addEventListener('focus', refresh)
    window.addEventListener(LICENSE_REFRESH_EVENT, refresh)
    return () => {
      mounted.current = false
      window.removeEventListener('focus', refresh)
      window.removeEventListener(LICENSE_REFRESH_EVENT, refresh)
    }
  }, [fetchLicense])

  useEffect(() => {
    if (!license.valid || !license.expires) return
    const deadline = licenseExpiryMillis(license.expires)
    if (!Number.isFinite(deadline)) return
    let timer: ReturnType<typeof setTimeout>
    let cancelled = false
    const schedule = () => {
      const remaining = deadline - Date.now()
      // Clamp long-lived licenses to the browser timer limit. A browser whose
      // clock is ahead retries once a minute rather than looping on stale data.
      timer = setTimeout(async () => {
        if (Date.now() >= deadline) await fetchLicense()
        if (!cancelled) schedule()
      }, remaining > 0 ? Math.min(remaining + 1, 2147483647) : 60000)
    }
    schedule()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [license.valid, license.expires, fetchLicense])

  // An ended trial or expired license keeps its tier name but grants nothing (O5).
  const isPro = license.valid && ['pro', 'education', 'enterprise'].includes(license.tier)
  const isEducation = license.valid && ['education', 'enterprise'].includes(license.tier)
  const isEnterprise = license.valid && license.tier === 'enterprise'
  const effectiveFeatures = featuresFromLicense(license.features)
  const hasFeature = (feature: string) => effectiveFeatures.has(feature)

  const value: LicenseContextValue = {
    ...license, isPro, isEducation, isEnterprise, hasFeature,
    isProPage: (page) => PRO_PAGES.includes(page),
    isProSettingsTab: (tab) => PRO_SETTINGS_TABS.includes(tab),
    refresh: fetchLicense,
    atPrinterLimit: (count) => license.max_printers !== null && count >= license.max_printers,
    maxPrinters: license.max_printers,
    atUserLimit: (count) => Number.isFinite(license.max_users) && count >= license.max_users,
    maxUsers: license.max_users,
  }

  return <LicenseContext.Provider value={value}>{children}</LicenseContext.Provider>
}

export function useLicense() {
  return useContext(LicenseContext)
}

export default LicenseContext
