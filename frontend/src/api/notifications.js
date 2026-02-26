import { fetchAPI } from './client.js'

export const alerts = {
  list: (params = {}) => {
    const query = new URLSearchParams();
    if (params.severity) query.set('severity', params.severity);
    if (params.alert_type) query.set('alert_type', params.alert_type);
    if (params.is_read !== undefined) query.set('is_read', params.is_read);
    if (params.limit) query.set('limit', params.limit);
    if (params.offset) query.set('offset', params.offset);
    const qs = query.toString();
    return fetchAPI(`/alerts${qs ? '?' + qs : ''}`);
  },
  unreadCount: () => fetchAPI('/alerts/unread-count'),
  summary: () => fetchAPI('/alerts/summary'),
  markRead: (id) => fetchAPI(`/alerts/${id}/read`, { method: 'PATCH' }),
  markAllRead: () => fetchAPI('/alerts/mark-all-read', { method: 'POST' }),
  dismiss: (id) => fetchAPI(`/alerts/${id}/dismiss`, { method: 'PATCH' }),
};

export const alertPreferences = {
  get: () => fetchAPI('/alert-preferences'),
  update: (preferences) => fetchAPI('/alert-preferences', {
    method: 'PUT',
    body: JSON.stringify({ preferences }),
  }),
};

export const pushNotifications = {
  getVapidKey: () => fetchAPI('/push/vapid-key'),
  subscribe: (data) => fetchAPI('/push/subscribe', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  unsubscribe: () => fetchAPI('/push/subscribe', { method: 'DELETE' }),
};
