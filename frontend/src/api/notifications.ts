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
    if (params.limit) query.set('limit', String(params.limit));
    if (params.offset) query.set('offset', String(params.offset));
    const qs = query.toString();
    return fetchAPI(`/alerts${qs ? '?' + qs : ''}`);
  },
  unreadCount: (): Promise<UnreadCount> => fetchAPI('/alerts/unread-count'),
  summary: (): Promise<AlertSummary> => fetchAPI('/alerts/summary'),
  markRead: (id: number): Promise<void> => fetchAPI(`/alerts/${id}/read`, { method: 'PATCH' }),
  markAllRead: (): Promise<void> => fetchAPI('/alerts/mark-all-read', { method: 'POST' }),
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
