import { useState } from 'react'
import { Bell, Settings as SettingsIcon, Users, Shield, Palette, Key, Webhook, Database, Eye, ScrollText } from 'lucide-react'
import Admin from './Admin'
import Permissions from './Permissions'
import Branding from './Branding'
import OIDCSettings from '../../components/admin/OIDCSettings'
import WebhookSettings from '../../components/admin/WebhookSettings'
import GroupManager from '../../components/admin/GroupManager'
import MFASetup from '../../components/auth/MFASetup'
import APITokenManager from '../../components/auth/APITokenManager'
import SessionManager from '../../components/auth/SessionManager'
import QuotaManager from '../../components/admin/QuotaManager'
import IPAllowlistSettings from '../../components/admin/IPAllowlistSettings'
import OrgManager from '../../components/admin/OrgManager'
import LogViewer from '../../components/admin/LogViewer'
import NotificationsTab from '../../components/admin/NotificationsTab'
import GeneralTab from '../../components/admin/GeneralTab'
import VisionSettingsTab from '../../components/admin/VisionSettingsTab'
import SystemTab from '../../components/admin/SystemTab'
import { useLicense } from '../../LicenseContext'
import ProBadge from '../../components/shared/ProBadge'
import { pricingConfig } from '../../api'
import { useEffect } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

function AccessAccordion({ title, icon: Icon, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border border-farm-800 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-3 bg-farm-900 hover:bg-farm-800/70 transition-colors text-left"
      >
        {Icon && <Icon size={16} className="text-print-400" />}
        <span className="font-medium text-sm">{title}</span>
        {open ? <ChevronDown size={14} className="ml-auto text-farm-500" /> : <ChevronRight size={14} className="ml-auto text-farm-500" />}
      </button>
      {open && <div className="p-4 md:p-6 border-t border-farm-800">{children}</div>}
    </div>
  )
}

export default function Settings() {
  const [activeTab, setActiveTab] = useState('general')
  const lic = useLicense()
  const [uiMode, setUiMode] = useState('advanced')

  useEffect(() => {
    pricingConfig.get()
      .then(d => { if (d.ui_mode) setUiMode(d.ui_mode) }).catch(() => {})

    const handleUiModeChange = (e) => setUiMode(e.detail)
    window.addEventListener('ui-mode-changed', handleUiModeChange)
    return () => window.removeEventListener('ui-mode-changed', handleUiModeChange)
  }, [])

  const PRO_TABS = ['access', 'integrations', 'branding']
  const ALL_TABS = [
    { id: 'general', label: 'General', icon: SettingsIcon },
    { id: 'notifications', label: 'Notifications', icon: Bell },
    { id: 'access', label: 'Access', icon: Users },
    { id: 'integrations', label: 'Integrations', icon: Webhook },
    ...(uiMode === 'advanced' ? [{ id: 'vision', label: 'Vigil AI', icon: Eye }] : []),
    { id: 'branding', label: 'Branding', icon: Palette },
    { id: 'system', label: 'System', icon: Database },
    { id: 'logs', label: 'Logs', icon: ScrollText },
  ]
  const TABS = ALL_TABS.map(t => ({
    ...t,
    disabled: !lic.isPro && PRO_TABS.includes(t.id),
  }))

  return (
    <div className="p-4 md:p-6">
      <div className="mb-4 md:mb-6">
        <div className="flex items-center gap-3">
          <SettingsIcon className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Settings</h1>
            <p className="text-farm-500 text-sm mt-1">Configure your print farm</p>
          </div>
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 mb-6 overflow-x-auto pb-1 -mx-1 px-1">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => !tab.disabled && setActiveTab(tab.id)}
            disabled={tab.disabled}
            className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              tab.disabled
                ? 'bg-farm-900/50 text-farm-600 cursor-not-allowed'
                : activeTab === tab.id
                  ? 'bg-print-600 text-white'
                  : 'bg-farm-900 text-farm-400 hover:bg-farm-800 hover:text-farm-200'
            }`}
          >
            <tab.icon size={16} />
            <span className="hidden sm:inline">{tab.label}</span>
            {tab.disabled && <ProBadge />}
          </button>
        ))}
      </div>

      {/* ==================== ACCESS TAB (Users + Permissions + SSO + MFA) ==================== */}
      {activeTab === 'access' && <div className="max-w-4xl space-y-3">
        <AccessAccordion title="Users & Groups" icon={Users} defaultOpen>
          <Admin />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <GroupManager />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Permissions" icon={Shield}>
          <Permissions />
        </AccessAccordion>
        <AccessAccordion title="Authentication (OIDC & MFA)" icon={Key}>
          <OIDCSettings />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <MFASetup />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Tokens & Sessions" icon={Key}>
          <APITokenManager />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <SessionManager />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Quotas & Restrictions" icon={Shield}>
          <QuotaManager />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <IPAllowlistSettings />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Organizations" icon={Users}>
          <OrgManager />
        </AccessAccordion>
      </div>}

      {/* ==================== INTEGRATIONS TAB (Webhooks) ==================== */}
      {activeTab === 'integrations' && <div className="max-w-4xl">
        <WebhookSettings />
      </div>}

      {/* ==================== BRANDING TAB ==================== */}
      {activeTab === 'branding' && <Branding />}

      {/* ==================== NOTIFICATIONS TAB ==================== */}
      {activeTab === 'notifications' && <NotificationsTab />}

      {/* ==================== GENERAL TAB ==================== */}
      {activeTab === 'general' && <GeneralTab />}

      {/* ==================== VISION AI TAB ==================== */}
      {activeTab === 'vision' && <VisionSettingsTab />}

      {/* ==================== SYSTEM TAB ==================== */}
      {activeTab === 'system' && <SystemTab />}

      {/* ==================== LOGS TAB ==================== */}
      {activeTab === 'logs' && <div className="max-w-6xl">
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
          <div className="flex items-center gap-2 md:gap-3 mb-4">
            <ScrollText size={18} className="text-print-400" />
            <h2 className="text-base md:text-lg font-semibold">Application Logs</h2>
          </div>
          <LogViewer />
        </div>
      </div>}
    </div>
  )
}
