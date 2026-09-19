import { fetchAPI } from './client'

export type OIDCProviderType = 'microsoft' | 'google' | 'generic'

export interface OIDCConfig {
  configured?: boolean
  is_enabled: boolean
  display_name: string
  client_id: string
  tenant_id: string
  discovery_url: string
  scopes: string
  auto_create_users: boolean
  default_role: 'viewer'
  default_group_id: number | null
  provider_type: OIDCProviderType
  allowed_domains: string
  has_client_secret: boolean
}

export type OIDCConfigUpdate = Omit<OIDCConfig, 'configured' | 'has_client_secret'> & {
  client_secret?: string
}

export const oidc = {
  getConfig: (): Promise<OIDCConfig | { configured: false }> => fetchAPI('/admin/oidc'),
  updateConfig: (config: OIDCConfigUpdate): Promise<{ success: true }> => fetchAPI('/admin/oidc', {
    method: 'PUT',
    body: JSON.stringify(config),
  }),
}
