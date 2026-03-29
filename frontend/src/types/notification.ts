// Notification domain types

export type AlertType =
  | 'print_complete'
  | 'print_failed'
  | 'printer_error'
  | 'spool_low'
  | 'maintenance_overdue'
  | 'job_submitted'
  | 'job_approved'
  | 'job_rejected'
  | 'spaghetti_detected'
  | 'first_layer_issue'
  | 'detachment_detected'
  | 'bed_cooled'
  | 'queue_added'
  | 'queue_skipped'
  | 'queue_failed_start';

export type AlertSeverity = 'info' | 'warning' | 'critical';

export interface Alert {
  id: number;
  user_id: number;
  alert_type: AlertType;
  severity: AlertSeverity;
  title: string;
  message: string | null;
  is_read: boolean;
  is_dismissed: boolean;
  printer_id: number | null;
  job_id: number | null;
  spool_id: number | null;
  metadata_json: Record<string, unknown> | null;
  created_at: string;
  printer_name: string | null;
  job_name: string | null;
  spool_name: string | null;
}

export interface AlertSummary {
  print_failed: number;
  spool_low: number;
  maintenance_overdue: number;
  total: number;
}

export interface UnreadCount {
  count: number;
}

export interface AlertListParams {
  severity?: AlertSeverity;
  alert_type?: AlertType;
  is_read?: boolean;
  limit?: number;
  offset?: number;
}

export interface AlertPreference {
  alert_type: AlertType;
  in_app: boolean;
  browser_push: boolean;
  email: boolean;
  threshold_value: number | null;
}

export interface AlertPreferenceResponse extends AlertPreference {
  id: number;
  user_id: number;
}

export interface AlertPreferencesUpdate {
  preferences: AlertPreference[];
}

// ---- Push Notifications ----

export interface VapidKeyResponse {
  public_key: string;
}

export interface PushSubscriptionCreate {
  endpoint: string;
  p256dh_key: string;
  auth_key: string;
}

// ---- SMTP ----

export interface SmtpConfig {
  enabled: boolean;
  host: string;
  port: number;
  username: string;
  password: string;
  from_address: string;
  use_tls: boolean;
}

export interface SmtpConfigResponse {
  enabled: boolean;
  host: string;
  port: number;
  username: string;
  password_set: boolean;
  from_address: string;
  use_tls: boolean;
}
