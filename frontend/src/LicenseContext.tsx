import { createContext, useContext, useState, useEffect } from 'react'
import { fetchAPI } from './api'

const LicenseContext = createContext({
  tier: 'community',
  licensee: null,
  expires: null,
  loading: true,
  isPro: false,
  isEducation: false,
  isEnterprise: false,
  hasFeature: () => false,
  refresh: () => {},
})

export function featuresFromLicense(features) {
  return new Set(Array.isArray(features) ? features : [])
}

export const PRO_PAGES = ['orders', 'products', 'analytics', 'maintenance', 'permissions', 'branding']
export const PRO_SETTINGS_TABS = ['sso', 'webhooks', 'smtp']

export function LicenseProvider({ children }) {
  const [license, setLicense] = useState({
    tier: 'community', licensee: null, expires: null, max_printers: 5, max_users: 1,
    installation_id: null, features: [], managed_externally: false, loading: true,
  })

  const fetchLicense = async () => {
    try {
      const data = await fetchAPI('/license')
      setLicense({
        tier: data.tier || 'community',
        licensee: data.licensee || null,
        expires: data.expires || null,
        max_printers: data.max_printers ?? 5,
        max_users: data.max_users ?? 1,
        installation_id: data.installation_id || null,
        features: Array.isArray(data.features) ? data.features : [],
        managed_externally: data.managed_externally === true,
        loading: false,
      })
    } catch {
      setLicense({
        tier: 'community', licensee: null, expires: null, max_printers: 5, max_users: 1,
        installation_id: null, features: [], managed_externally: false, loading: false,
      })
    }
  }

  useEffect(() => { fetchLicense() }, [])

  const isPro = ['pro', 'education', 'enterprise'].includes(license.tier)
  const isEducation = ['education', 'enterprise'].includes(license.tier)
  const isEnterprise = license.tier === 'enterprise'
  const effectiveFeatures = featuresFromLicense(license.features)
  const hasFeature = (feature) => effectiveFeatures.has(feature)

  const value = {
    ...license, isPro, isEducation, isEnterprise, hasFeature,
    isProPage: (page) => PRO_PAGES.includes(page),
    isProSettingsTab: (tab) => PRO_SETTINGS_TABS.includes(tab),
    refresh: fetchLicense,
    atUserLimit: (count) => Number.isFinite(license.max_users) && count >= license.max_users,
    maxUsers: license.max_users,
  }

  return <LicenseContext.Provider value={value}>{children}</LicenseContext.Provider>
}

export function useLicense() {
  return useContext(LicenseContext)
}

export default LicenseContext
