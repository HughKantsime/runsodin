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

const TIER_FEATURES = {
  community: new Set([
    'dashboard', 'printers', 'cameras', 'jobs', 'upload',
    'models', 'spools', 'timeline', 'calculator',
    'keyboard_shortcuts', 'pwa', 'i18n', '3d_viewer',
  ]),
  pro: new Set([
    'dashboard', 'printers', 'cameras', 'jobs', 'upload',
    'models', 'spools', 'timeline', 'calculator',
    'unlimited_printers', 'unlimited_users',
    'rbac', 'sso', 'white_label', 'branding',
    'orders', 'products', 'bom',
    'webhooks', 'email_notifications',
    'analytics', 'csv_export',
    'maintenance', 'care_counters',
    'prometheus', 'mqtt_republish',
    'quiet_hours', 'permissions',
    'smart_plug', 'energy_tracking', 'ams_environment', 'websocket', 'drag_drop_queue', 'ntfy', 'telegram', 'hms_decoder', 'failure_logging',
    'usage_reports',
  ]),
  education: new Set([
    'job_approval', 'print_quotas', 'class_sections',
  ]),
  enterprise: new Set([
    'opcua', 'mqtt_republish_enterprise', 'audit_export', 'sqlcipher',
    'custom_integrations',
  ]),
}

function getFeaturesForTier(tier) {
  const tiers = ['community', 'pro', 'education', 'enterprise']
  const idx = tiers.indexOf(tier)
  if (idx === -1) return TIER_FEATURES.community
  const features = new Set()
  for (let i = 0; i <= idx; i++) {
    const tf = TIER_FEATURES[tiers[i]]
    if (tf) tf.forEach(f => features.add(f))
  }
  return features
}

export const PRO_PAGES = ['orders', 'products', 'analytics', 'maintenance', 'permissions', 'branding']
export const PRO_SETTINGS_TABS = ['sso', 'webhooks', 'smtp']

export function LicenseProvider({ children }) {
  const [license, setLicense] = useState({
    tier: 'community', licensee: null, expires: null, loading: true,
  })

  const fetchLicense = async () => {
    try {
      const data = await fetchAPI('/license')
      setLicense({
        tier: data.tier || 'community',
        licensee: data.licensee || null,
        expires: data.expires || null,
        loading: false,
      })
    } catch {
      setLicense(prev => ({ ...prev, tier: 'community', loading: false }))
    }
  }

  useEffect(() => { fetchLicense() }, [])

  const isPro = ['pro', 'education', 'enterprise'].includes(license.tier)
  const isEducation = ['education', 'enterprise'].includes(license.tier)
  const isEnterprise = license.tier === 'enterprise'
  const hasFeature = (feature) => getFeaturesForTier(license.tier).has(feature)

  const value = {
    ...license, isPro, isEducation, isEnterprise, hasFeature,
    isProPage: (page) => PRO_PAGES.includes(page),
    isProSettingsTab: (tab) => PRO_SETTINGS_TABS.includes(tab),
    refresh: fetchLicense,
    atUserLimit: (count) => license.tier === "community" && count >= 3,
    maxUsers: license.tier === "community" ? 3 : Infinity,
  }

  return <LicenseContext.Provider value={value}>{children}</LicenseContext.Provider>
}

export function useLicense() {
  return useContext(LicenseContext)
}

export default LicenseContext
