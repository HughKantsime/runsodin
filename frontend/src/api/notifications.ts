import { fetchAPI } from './client'
import type {
  Alert,
  AlertSummary,
  UnreadCount,
  AlertListParams,
  AlertPreference,
  AlertPreferenceResponse,
  VapidKeyResponse,
  PushSubscriptionCreate,
} from '../types'

export const alerts = {
  list: (params: AlertListParams = {}): Promise<Alert[]> => {
    const query = new URLSearchParams();
    if (params.severity) query.set('severity', params.severity);
    if (params.alert_type) query.set('alert_type', params.alert_type);
    if (params.is_read !== undefined) query.set('is_read', String(params.is_read));
    if (params.is_dismissed !== undefined) query.set('is_dismissed', String(params.is_dismissed));
    if (params.include_dismissed) query.set('include_dismissed', 'true');
    if (params.limit) query.set('limit', String(params.limit));
    if (params.offset) query.set('offset', String(params.offset));
    const qs = query.toString();
    return fetchAPI(`/alerts${qs ? '?' + qs : ''}`);
  },
  unreadCount: (): Promise<UnreadCount> => fetchAPI('/alerts/unread-count'),
  summary: (): Promise<AlertSummary> => fetchAPI('/alerts/summary'),
  markRead: (id: number): Promise<void> => fetchAPI(`/alerts/${id}/read`, { method: 'PATCH' }),
  markAllRead: (): Promise<void> => fetchAPI('/alerts/mark-all-read', { method: 'POST' }),
  remove: async (id: number): Promise<{status: 'ok'; alert_id: number}> => {
    const receipt = await fetchAPI<{status: string; alert_id: number}>(`/alerts/${id}`, { method: 'DELETE' })
    if (receipt?.status !== 'ok' || receipt.alert_id !== id) throw new Error('Deletion was not confirmed')
    return {status: 'ok', alert_id: id}
  },
  dismiss: (id: number): Promise<void> => fetchAPI(`/alerts/${id}/dismiss`, { method: 'PATCH' }),
};

export const alertPreferences = {
  get: (): Promise<AlertPreferenceResponse[]> => fetchAPI('/alert-preferences'),
  update: (preferences: AlertPreference[]): Promise<void> => fetchAPI('/alert-preferences', {
    method: 'PUT',
    body: JSON.stringify({ preferences }),
  }),
};

export const pushNotifications = {
  getVapidKey: (): Promise<VapidKeyResponse> => fetchAPI('/push/vapid-key'),
  subscribe: (data: PushSubscriptionCreate): Promise<void> => fetchAPI('/push/subscribe', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  unsubscribe: (): Promise<void> => fetchAPI('/push/subscribe', { method: 'DELETE' }),
};

export const pushKeys = {
  /** Superadmin: replace the VAPID pair; every browser subscription must re-register. */
  rotate: (): Promise<{ status: string; public_key: string; subscriptions_invalidated: number }> =>
    fetchAPI('/push/vapid-key/rotate', { method: 'POST' }),
};

export type PushTestStatus = 'sent' | 'not_configured' | 'bad_device_token' | 'topic_mismatch' | 'auth_error' | 'other' | 'education_scope'
export interface PushTestDevice { device_id: number; status: PushTestStatus; reason: string | null; apns_id: string | null; token_fingerprint: string }
export interface PushTestResult { sent: number; total_devices: number; devices: PushTestDevice[] }

export const pushTest = {
  /** Admin: send one test notification to each phone registered to the signed-in user (ignores quiet hours). */
  send: (): Promise<PushTestResult> => fetchAPI('/push/test', { method: 'POST' }),
};

const PUSH_TEST_COPY: Record<PushTestStatus, string> = {
  sent: 'Sent. Check the phone.',
  not_configured: 'Not sent: phone push is unavailable or not set up on this server.',
  bad_device_token: 'Not sent: Apple no longer accepts this phone. Open the ODIN app on it to register again.',
  topic_mismatch: "Not sent: the app's bundle ID does not match the server's Apple push setting.",
  auth_error: "Not sent: Apple rejected the server's push key.",
  education_scope: 'Not sent: push is turned off for education accounts.',
  other: 'Not sent: the test could not be sent because of an unexpected error.',
}

/** Plain-words line for one device's test result. */
export function describePushTestDevice(device: PushTestDevice): string {
  const phone = device.token_fingerprint ? `Phone ending ${device.token_fingerprint}` : `Phone ${device.device_id}`
  const reason = device.status !== 'sent' && device.reason ? ` (${device.reason})` : ''
  return `${phone}: ${PUSH_TEST_COPY[device.status] ?? PUSH_TEST_COPY.other}${reason}`
}
