// User, auth, and organization domain types

export type UserRole = 'admin' | 'operator' | 'viewer' | 'student';

export interface User {
  id: number;
  username: string;
  email: string;
  role: string;
  is_active: boolean;
  created_at: string;
  group_id?: number | null;
  org_id?: number | null;
  mfa_enabled?: boolean;
  [key: string]: unknown;
}

export interface UserCreate {
  username: string;
  email: string;
  password?: string;
  role?: string;
  group_id?: number;
  send_welcome_email?: boolean;
}

export interface UserCreateResult {
  status: 'created';
  welcome_email?: 'sent' | 'unconfirmed';
  message?: string;
}

export interface UserUpdate {
  username?: string;
  email?: string;
  role?: string;
  is_active?: boolean;
  group_id?: number | null;
  password?: string;
}

// ---- Auth ----

export interface LoginResponse {
  access_token?: string;
  token_type?: string;
  mfa_required?: boolean;
  mfa_setup_required?: boolean;
  mfa_token?: string;
  user?: User;
}

export interface MfaSetupResponse {
  secret: string;
  qr_uri: string;
}

export interface MfaStatus {
  enabled: boolean;
}

export interface WsTokenResponse {
  token: string;
}

export interface AuthCapabilities {
  oidc_enabled: boolean;
  oidc_provider_name: string | null;
  local_auth_enabled: boolean;
  [key: string]: unknown;
}

// ---- Session ----

export interface Session {
  id: number;
  user_id: number;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
  last_active: string | null;
  is_current: boolean;
}

// ---- API Token ----

export interface ApiToken {
  id: number;
  name: string;
  token_prefix: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
}

export interface ApiTokenCreate {
  name: string;
  scopes?: string[];
  expires_days?: number | null;
}

export interface ApiTokenCreateResponse extends ApiToken {
  token: string; // Full token, only returned on creation
}

// ---- Organization ----

export interface Organization {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  member_count: number;
  printer_count: number;
  [key: string]: unknown;
}

export interface OrgCreate {
  name: string;
  description?: string;
}

export interface OrgUpdate {
  name?: string;
  description?: string;
}

export interface OrgSettings {
  [key: string]: unknown;
}

// ---- Group ----

export interface Group {
  id: number;
  name: string;
  description: string | null;
  org_id: number | null;
  created_at: string;
  updated_at: string;
  member_count: number;
  [key: string]: unknown;
}

export interface GroupCreate {
  name: string;
  description?: string;
  org_id?: number;
}

export interface GroupUpdate {
  name?: string;
  description?: string;
}

// ---- License ----

/** Exact unauthenticated capability response; customer/device identity is absent. */
export interface PublicLicense {
  valid: boolean;
  tier: string;
  tier_name: string;
  issued_at: string;
  expires_at: string | null;
  expired: boolean;
  /** Signed Pro trial license (portal 14-day trial); stays true after it ends. */
  trial: boolean;
  /** Whole days left on a trial (0 once ended); null when not a trial. */
  trial_days_left: number | null;
  max_printers: number | null;
  max_users: number | null;
  features: string[];
  managed_externally: boolean;
  binding_present: boolean;
  binding_matches_current: boolean;
  license_sha256: string;
}

export interface License {
  valid: boolean;
  tier: string;
  features: string[];
  expires_at: string | null;
  managed_externally?: boolean;
  [key: string]: unknown;
}

// ---- Permissions ----

export interface PermissionsConfig {
  [key: string]: unknown;
}

// ---- Quotas ----

export interface Quota {
  user_id: number;
  daily_print_limit: number | null;
  monthly_print_limit: number | null;
  daily_used: number;
  monthly_used: number;
  [key: string]: unknown;
}

export interface QuotaUpdate {
  daily_print_limit?: number | null;
  monthly_print_limit?: number | null;
}

// ---- IP Allowlist ----

export interface IpAllowlistConfig {
  enabled: boolean;
  allowed_ips: string[];
}

// ---- Audit Logs ----

export interface AuditLog {
  id: number;
  user_id?: number | null;
  username?: string | null;
  auth_kind?: string | null;
  entity_type: string | null;
  entity_id: number | null;
  action: string;
  details: Record<string, unknown> | unknown[] | string | number | boolean | null;
  ip_address: string | null;
  timestamp: string | null;
}

export interface AuditLogParams {
  limit?: number;
  offset?: number;
  entity_type?: string;
  action?: string;
  date_from?: string;
  date_to?: string;
  q?: string;
}

export interface AuditLogPage {
  logs: AuditLog[];
  total: number;
  limit: number;
  offset: number;
}

// ---- Setup ----

export interface SetupStatus {
  needs_setup: boolean;
  has_users: boolean;
  is_complete: boolean;
}

// ---- GDPR ----

export interface GdprExportData {
  [key: string]: unknown;
}
