// One plain sentence per audit row. Pure: (action, details, actor) -> sentence.
// An action missing from SENTENCES returns null and the page shows the raw row.
// Only named fields are read: a sentence never dumps details, so it cannot show
// a secret. Setting changes name the setting, not its value (retention is the
// exception: its values are shown as stored).

export interface AuditContext {
  /** The row's entity_id (the job, printer, user, file... the action is about). */
  entityId?: number | null
  /** Optional printer id -> name lookup; without it a printer is "printer 3". */
  printerName?: (id: number) => string | undefined
}

type Details = Record<string, unknown>
type Writer = (d: Details, who: string, ctx: AuditContext, raw: unknown) => string

const text = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() ? value.trim() : typeof value === 'number' ? String(value) : null
const names = (value: unknown): string =>
  Array.isArray(value) ? value.filter(v => typeof v === 'string' && v).join(', ') : ''
const withNames = (sentence: string, value: unknown) => (names(value) ? `${sentence}: ${names(value)}` : sentence)
const a = (noun: string) => `${/^[aeiou]/i.test(noun) ? 'an' : 'a'} ${noun}`
const thing = (noun: string, id: unknown) => (text(id) !== null ? `${noun} ${text(id)}` : a(noun))
const printer = (id: unknown, ctx: AuditContext) =>
  (typeof id === 'number' && ctx.printerName?.(id)) || thing('printer', id)
const named = (noun: string, name: unknown, id: unknown) => (text(name) ? `${noun} ${text(name)}` : thing(noun, id))
/** Legacy rows store one string, e.g. "Login: ada from 10.0.0.5" or "Token 'ci' created". */
const between = (raw: unknown, pattern: RegExp) => (typeof raw === 'string' ? pattern.exec(raw)?.[1] ?? null : null)
const account = (name: string | null) => (!name ? 'an unknown account' : name === '[deleted]' ? 'a deleted account' : name)

const EXPORTS: Record<string, string> = {
  jobs: 'the jobs list', spools: 'the spools list', models: 'the models list', 'filament-usage': 'the filament usage report',
  'audit-logs': 'the audit log', 'audit-jsonl': 'the signed audit log', 'archive-print-log': 'the print log',
}
const FILE_PARTS: Record<string, string> = { mesh: 'the 3D mesh of', model: 'the model file of', plate_thumbnail: 'a plate picture of' }

const dispatch = (verb: string): Writer => (d, who, ctx) =>
  `${d.trigger === 'scheduler' ? 'The scheduler' : who} ${verb} ${thing('job', ctx.entityId)} to ${printer(d.printer_id, ctx)}`
const license = (verb: string): Writer => (d, who) =>
  `${who} ${verb} ${text(d.tier) ? `${a(text(d.tier)!)} license` : 'a license'}${text(d.licensee) ? ` for ${text(d.licensee)}` : ''}`

export const SENTENCES: Record<string, Writer> = {
  // Sign-in
  'auth.login': (d, who) => `${who === 'Someone' ? text(d.username) ?? who : who} signed in${d.method === 'oidc' ? ' with single sign-on' : ''}`,
  'auth.login_failed': d => `Sign-in failed for ${account(text(d.username))}${d.method === 'oidc' ? ' (single sign-on)' : ''}`,
  'auth.logout': (d, who) => `${who === 'Someone' ? text(d.username) ?? who : who} signed out`,
  login_success: (_d, _who, _ctx, raw) => `Password accepted for ${account(between(raw, /^Login: (.+?)(?: from .*)?$/))}`,
  login_failed: (_d, _who, _ctx, raw) => `Sign-in failed for ${account(between(raw, /^Failed login: (.+?)(?: from .*)?$/))}`,
  mfa_enabled: (_d, who) => `${who} turned on two-step sign-in`,
  mfa_disabled: (_d, who) => `${who} turned off two-step sign-in`,
  mfa_disabled_admin: (_d, who, ctx) => `${who} turned off two-step sign-in for ${thing('user', ctx.entityId)}`,
  session_revoked_admin: (_d, who, _ctx, raw) => `${who} ended a session of ${thing('user', between(raw, /user_id=(\d+)/))}`,

  // Files, downloads and exports
  print_file_uploaded: (d, who, ctx) =>
    `${who} uploaded ${thing('print file', ctx.entityId)}${d.target_printer_id != null ? ` for ${printer(d.target_printer_id, ctx)}` : ''}`,
  'print_file.downloaded': (d, who, ctx) =>
    `${who} downloaded ${FILE_PARTS[String(d.what)] ?? ''} ${thing('print file', ctx.entityId)}`.replace(/\s+/g, ' '),
  'export.downloaded': (d, who) => `${who} exported ${EXPORTS[String(d.export)] ?? (text(d.export) ? `"${text(d.export)}"` : 'data')}`,
  'backup.downloaded': (d, who) => `${who} downloaded ${named('backup', d.filename, null)}`,
  backup_created: (d, who) => `${who} created ${named('backup', d.filename, null)}`,
  backup_deleted: (d, who) => `${who} deleted ${named('backup', d.filename, null)}`,
  'timelapse.downloaded': (_d, who, ctx) => `${who} downloaded ${thing('timelapse', ctx.entityId)}`,
  'timelapse.viewed': (_d, who, ctx) => `${who} watched ${thing('timelapse', ctx.entityId)}`,
  'camera.viewed': (_d, who, ctx) => `${who} viewed the camera on ${printer(ctx.entityId, ctx)}`,
  'profile.exported': (_d, who, ctx) => `${who} exported ${thing('profile', ctx.entityId)}`,
  'project.exported': (_d, who, ctx) => `${who} exported ${thing('project', ctx.entityId)}`,

  // Jobs
  'job.created': (d, who, ctx) =>
    `${who} created ${thing('job', ctx.entityId)}${text(d.item_name) ? ` (${text(d.item_name)})` : ''}${d.status === 'submitted' ? ' and sent it for approval' : ''}`,
  'job.dispatched': dispatch('sent'),
  'job.dispatch_requested': dispatch('asked to send'),
  'job.cancelled': (_d, who, ctx) => `${who} cancelled ${thing('job', ctx.entityId)}`,
  'job.approved': (_d, who, ctx) => `${who} approved ${thing('job', ctx.entityId)}`,
  'job.rejected': (d, who, ctx) => `${who} rejected ${thing('job', ctx.entityId)}${text(d.reason) ? `: ${text(d.reason)}` : ''}`,
  'job.deleted': (d, who, ctx) => `${who} deleted ${thing('job', ctx.entityId)}${text(d.item_name) ? ` (${text(d.item_name)})` : ''}`,

  // Printers
  'printer.created': (d, who, ctx) => `${who} added ${named('printer', d.name, ctx.entityId)}`,
  'printer.updated': (d, who, ctx) => withNames(`${who} edited ${printer(ctx.entityId, ctx)}`, d.fields),
  'printer.deleted': (d, who, ctx) => `${who} removed ${named('printer', d.name, ctx.entityId)}`,

  // Settings: the setting's name, never its value
  'config.updated': (d, who) => withNames(`${who} changed settings`, d.keys),
  'branding.updated': (d, who) => withNames(`${who} changed branding`, d.fields),
  oidc_config_updated: (d, who) => withNames(`${who} changed single sign-on settings`, d.fields),
  smtp_config_updated: (_d, who) => `${who} changed email (SMTP) settings`,
  ip_allowlist_updated: (_d, who) => `${who} changed the IP allowlist`,
  permissions_updated: (_d, who) => `${who} changed role permissions`,
  education_mode_toggled: (_d, who, _ctx, raw) => `${who} turned Education mode ${/enabled/.test(String(raw)) ? 'on' : 'off'}`,
  retention_updated: (_d, who, _ctx, raw) => {
    const stored = between(raw, /^Retention config: ([\s\S]+)$/)
    return stored ? `${who} changed data retention to ${stored}` : `${who} changed data retention`
  },

  // Users and tokens
  'user.created': (d, who, ctx) => `${who} created ${named('user', d.username, ctx.entityId)}${text(d.role) ? ` as ${text(d.role)}` : ''}`,
  'user.deleted': (d, who, ctx) => `${who} deleted ${named('user', d.deleted_username, ctx.entityId)}`,
  'user.role_changed': (d, who, ctx) => `${who} changed the role of ${thing('user', ctx.entityId)}${text(d.new_role) ? ` to ${text(d.new_role)}` : ''}`,
  'user.password_changed': (_d, who, ctx) => `${who} changed the password of ${thing('user', ctx.entityId)}`,
  'user.password_reset': (_d, _who, ctx) => `The password of ${thing('user', ctx.entityId)} was reset by email link`,
  api_token_created: (_d, who, ctx, raw) => `${who} created ${named('API token', between(raw, /^Token '(.*)' created$/), ctx.entityId)}`,
  api_token_revoked: (_d, who, ctx, raw) => `${who} revoked ${named('API token', between(raw, /^Token '(.*)' revoked$/), ctx.entityId)}`,

  // License
  'license.uploaded': license('installed'),
  'license.activated': license('activated'),
  'license.reactivated': license('reactivated'),
  'license.removed': (_d, who) => `${who} removed the license`,
  'license.unactivated': (_d, who) => `${who} deactivated the license`,
}

/** The sentence for a known action, or null: the caller then shows the raw row. */
export function auditSentence(action: unknown, details: unknown, actor?: string | null, ctx: AuditContext = {}): string | null {
  if (typeof action !== 'string' || !Object.prototype.hasOwnProperty.call(SENTENCES, action)) return null
  const fields = details && typeof details === 'object' && !Array.isArray(details) ? (details as Details) : {}
  try {
    return SENTENCES[action](fields, actor?.trim() || 'Someone', ctx, details)
  } catch {
    return null
  }
}
