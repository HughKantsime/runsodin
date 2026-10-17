import { createContext, useContext, useState, useEffect } from 'react'
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
  managed_externally: false, loading: true, loadError: false,
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

export function LicenseProvider({ children }: { children: ReactNode }) {
  const [license, setLicense] = useState<LicenseState>({
    tier: 'community', valid: false, licensee: null, expires: null, max_printers: 5, max_users: 1,
    installation_id: null, features: [], managed_externally: false, loading: true, loadError: false,
  })

  const fetchLicense = async () => {
    try {
      const data = await licenseAPI.get()
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
        loading: false,
        loadError: false,
      })
    } catch {
      setLicense({
        tier: 'community', valid: false, licensee: null, expires: null, max_printers: 5, max_users: 1,
        installation_id: null, features: [], managed_externally: false, loading: false, loadError: true,
      })
    }
  }

  useEffect(() => { fetchLicense() }, [])

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
