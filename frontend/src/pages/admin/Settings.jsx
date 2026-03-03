import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bell, Settings as SettingsIcon, Users, Shield, Palette, Key, Webhook, Database, Eye, ScrollText, FileText } from 'lucide-react'
import { ChevronDown, ChevronRight } from 'lucide-react'
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
import LicenseTab from '../../components/admin/LicenseTab'
import { useLicense } from '../../LicenseContext'
import ProBadge from '../../components/shared/ProBadge'
import { pricingConfig } from '../../api'
import { PageHeader, TabBar } from '../../components/ui'

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
  const { data: pricingData } = useQuery({
    queryKey: ['pricing-config'],
    queryFn: () => pricingConfig.get(),
    refetchInterval: 30000,
  })
  const uiMode = pricingData?.ui_mode || 'advanced'

  const PRO_TABS = ['access', 'integrations', 'branding']
  const ALL_TABS = [
    { id: 'general', label: 'General', icon: SettingsIcon },
    { id: 'notifications', label: 'Notifications', icon: Bell },
    { id: 'access', label: 'Access', icon: Users },
    { id: 'integrations', label: 'Integrations', icon: Webhook },
    ...(uiMode === 'advanced' ? [{ id: 'vision', label: 'Vigil AI', icon: Eye }] : []),
    { id: 'branding', label: 'Branding', icon: Palette },
    { id: 'license', label: 'License', icon: FileText },
    { id: 'system', label: 'System', icon: Database },
    { id: 'logs', label: 'Logs', icon: ScrollText },
  ]
  const TABS = ALL_TABS.map(t => ({
    ...t,
    disabled: !lic.isPro && PRO_TABS.includes(t.id),
  }))

  return (
    <div className="p-4 md:p-6">
      <PageHeader icon={SettingsIcon} title="Settings" subtitle="Configure your print farm" />

      {/* Tab Bar */}
      <div className="mb-6 overflow-x-auto pb-1 -mx-1 px-1">
        <TabBar
          tabs={TABS.map(t => ({ value: t.id, label: t.label, icon: t.icon }))}
          active={activeTab}
          onChange={(val) => {
            const tab = TABS.find(t => t.id === val)
            if (!tab?.disabled) setActiveTab(val)
          }}
        />
      </div>

      {/* ==================== GENERAL TAB ==================== */}
      {activeTab === 'general' && <GeneralTab />}

      {/* ==================== NOTIFICATIONS TAB ==================== */}
      {activeTab === 'notifications' && <NotificationsTab />}

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

      {/* ==================== VISION AI TAB ==================== */}
      {activeTab === 'vision' && <VisionSettingsTab />}

      {/* ==================== BRANDING TAB ==================== */}
      {activeTab === 'branding' && <Branding />}

      {/* ==================== LICENSE TAB ==================== */}
      {activeTab === 'license' && <div className="max-w-4xl"><LicenseTab /></div>}

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
