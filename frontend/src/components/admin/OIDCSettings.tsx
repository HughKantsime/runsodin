import { useEffect, useState } from 'react'
import { ExternalLink, Eye, EyeOff, Save, ShieldCheck } from 'lucide-react'
import { oidc, orgs, type OIDCConfig, type OIDCProviderType } from '../../api'
import type { Organization } from '../../types'
import { Button, Card, Input, Select, Switch } from '../ui'

const DEFAULT_CONFIG: OIDCConfig = {
  is_enabled: false,
  display_name: 'Microsoft Entra ID',
  client_id: '',
  tenant_id: '',
  discovery_url: '',
  scopes: 'openid profile email',
  auto_create_users: false,
  default_role: 'viewer',
  default_group_id: null,
  provider_type: 'microsoft',
  allowed_domains: '',
  has_client_secret: false,
}

const PROVIDERS: Record<OIDCProviderType, { name: string; discovery: string; help: string }> = {
  microsoft: {
    name: 'Microsoft Entra ID',
    discovery: '',
    help: 'https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app',
  },
  google: {
    name: 'Google Workspace',
    discovery: 'https://accounts.google.com/.well-known/openid-configuration',
    help: 'https://developers.google.com/identity/openid-connect/openid-connect',
  },
  generic: {
    name: 'Single Sign-On',
    discovery: '',
    help: 'https://openid.net/developers/how-connect-works/',
  },
}

export default function OIDCSettings() {
  const [config, setConfig] = useState<OIDCConfig>(DEFAULT_CONFIG)
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [clientSecret, setClientSecret] = useState('')
  const [showSecret, setShowSecret] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  useEffect(() => {
    Promise.all([oidc.getConfig(), orgs.list()])
      .then(([loaded, loadedOrganizations]) => {
        if (!('configured' in loaded && loaded.configured === false)) setConfig({ ...DEFAULT_CONFIG, ...loaded })
        setOrganizations(loadedOrganizations)
      })
      .catch((error) => setMessage({ type: 'error', text: error instanceof Error ? error.message : 'Unable to load SSO settings.' }))
      .finally(() => setLoading(false))
  }, [])

  const change = <K extends keyof OIDCConfig>(field: K, value: OIDCConfig[K]) => {
    setConfig((current) => ({ ...current, [field]: value }))
    setMessage(null)
  }

  const changeProvider = (provider: OIDCProviderType) => {
    const preset = PROVIDERS[provider]
    setConfig((current) => ({
      ...current,
      provider_type: provider,
      display_name: preset.name,
      discovery_url: preset.discovery,
      tenant_id: provider === 'microsoft' ? current.tenant_id : '',
      allowed_domains: provider === 'google' ? current.allowed_domains : '',
      scopes: 'openid profile email',
    }))
    setMessage(null)
  }

  const save = async () => {
    setSaving(true)
    setMessage(null)
    try {
      await oidc.updateConfig({ ...config, ...(clientSecret ? { client_secret: clientSecret } : {}) })
      setClientSecret('')
      setMessage({ type: 'success', text: 'Single sign-on configuration saved.' })
      const loaded = await oidc.getConfig()
      if (!('configured' in loaded && loaded.configured === false)) setConfig({ ...DEFAULT_CONFIG, ...loaded })
    } catch (error) {
      setMessage({ type: 'error', text: error instanceof Error ? error.message : 'Unable to save SSO settings.' })
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <p className="text-sm text-[var(--brand-text-secondary)]">Loading single sign-on settings…</p>

  const provider = PROVIDERS[config.provider_type]
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3">
        <ShieldCheck className="mt-0.5 shrink-0 text-[var(--brand-primary)]" size={22} aria-hidden="true" />
        <div>
          <h3 className="font-semibold text-[var(--brand-text-primary)]">Single sign-on</h3>
          <p className="mt-1 text-sm text-[var(--brand-text-secondary)]">
            Configure one identity provider for this ODIN installation. Google Classroom authorization is managed separately.
          </p>
        </div>
      </div>

      <Card className="space-y-4 border border-[var(--brand-card-border)]">
        <Switch
          label="Enable single sign-on"
          description={`Show “Sign in with ${config.display_name || provider.name}” on the login page.`}
          checked={config.is_enabled}
          onChange={(event) => change('is_enabled', event.target.checked)}
        />
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Select
          label="Identity provider"
          value={config.provider_type}
          onChange={(event) => changeProvider(event.target.value as OIDCProviderType)}
          options={[
            { value: 'microsoft', label: 'Microsoft Entra ID' },
            { value: 'google', label: 'Google Workspace' },
            { value: 'generic', label: 'Generic OpenID Connect' },
          ]}
        />
        <Input label="Login button label" value={config.display_name} onChange={(event) => change('display_name', event.target.value)} placeholder={provider.name} />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Input label="OAuth client ID" value={config.client_id} onChange={(event) => change('client_id', event.target.value)} autoComplete="off" className="font-mono" />
        <div>
          <Input
            label={config.has_client_secret ? 'OAuth client secret (configured)' : 'OAuth client secret'}
            type={showSecret ? 'text' : 'password'}
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
            autoComplete="new-password"
            placeholder={config.has_client_secret ? 'Leave blank to keep current secret' : 'Enter client secret'}
            className="pr-10 font-mono"
          />
          <Button type="button" variant="ghost" size="icon" aria-label={showSecret ? 'Hide client secret' : 'Show client secret'} onClick={() => setShowSecret((shown) => !shown)} className="float-right -mt-9 mr-1" icon={showSecret ? EyeOff : Eye} />
        </div>
      </div>

      {config.provider_type === 'microsoft' && (
        <Input label="Microsoft directory tenant ID" value={config.tenant_id} onChange={(event) => change('tenant_id', event.target.value)} placeholder="Directory tenant UUID or domain" className="font-mono" />
      )}

      {config.provider_type === 'google' && (
        <Input label="Allowed Google Workspace domains" value={config.allowed_domains} onChange={(event) => change('allowed_domains', event.target.value)} placeholder="ctechigh.org" className="font-mono" />
      )}

      <Input
        label="OIDC discovery URL"
        value={config.discovery_url}
        onChange={(event) => change('discovery_url', event.target.value)}
        placeholder={config.provider_type === 'microsoft' ? 'Leave blank for Microsoft commercial cloud' : 'https://provider.example/.well-known/openid-configuration'}
        className="font-mono text-xs"
      />

      <div className="grid gap-4 md:grid-cols-2">
        <Input label="Login scopes" value={config.scopes} onChange={(event) => change('scopes', event.target.value)} className="font-mono" />
        <Select label="ODIN tenant for SSO users" value={config.default_group_id ?? ''} onChange={(event) => change('default_group_id', event.target.value ? Number(event.target.value) : null)}>
          <option value="">Select a tenant</option>
          {organizations.map((organization) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}
        </Select>
      </div>

      <Card className="border border-[var(--brand-card-border)]">
        <Switch
          label="Create new SSO users"
          description="New identities are viewers in the selected ODIN tenant. Pre-rostered Google users are claimed only by verified Workspace email."
          checked={config.auto_create_users}
          onChange={(event) => change('auto_create_users', event.target.checked)}
        />
      </Card>

      {message && (
        <div role={message.type === 'error' ? 'alert' : 'status'} className={`rounded-md border p-3 text-sm ${message.type === 'error' ? 'border-[var(--status-failed)] text-[var(--status-failed)]' : 'border-[var(--status-completed)] text-[var(--status-completed)]'}`}>
          {message.text}
        </div>
      )}

      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
        <a href={provider.help} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-sm text-[var(--brand-primary)] hover:text-[var(--brand-accent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-primary)]">
          Provider setup documentation <ExternalLink size={14} aria-hidden="true" />
        </a>
        <Button icon={Save} loading={saving} onClick={save}>Save configuration</Button>
      </div>
    </div>
  )
}
