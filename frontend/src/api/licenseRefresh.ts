import { fetchAPI } from './client'
import type { PublicLicense } from '../types/user'

/** What the license portal said at the last "Refresh license". */
export type RefreshStatus = 'renewed' | 'not_renewed_yet' | 'canceled' | 'not_entitled'

export interface LastRefresh {
  status: RefreshStatus;
  /** UTC time of the refresh (ISO 8601). */
  at: string;
  /** The end date the license portal reported, or the installed file's end. */
  expires_at: string | null;
}

/** GET /api/license once a refresh has happened for the installed file. */
export type LicenseWithRefresh = PublicLicense & { last_refresh?: LastRefresh | null }

export interface RefreshResult {
  status: 'reactivated';
  refresh_status: RefreshStatus;
  /** "community" when the installed file has ended. */
  tier: string;
  valid: boolean;
  licensee?: string;
  expires_at: string | null;
  last_refresh: LastRefresh | null;
}

export const licenseRefresh = {
  /** "Refresh license": asks the license portal once. Superadmin only; the server enforces it. */
  reactivate: (): Promise<RefreshResult> => fetchAPI<RefreshResult>('/license/reactivate', { method: 'POST' }),
}
