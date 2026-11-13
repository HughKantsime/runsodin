import { describe, expect, test } from 'vitest'
import { auditSentence, SENTENCES } from './auditSentence'

const printerName = (id: number) => (id === 3 ? 'Test X1C' : undefined)
// [action, details, actor, entity id, sentence]
const CASES: [string, unknown, string | null, number | null, string][] = [
  ['auth.login', { username: 'ada' }, 'ada', 1, 'ada signed in'],
  ['auth.login_failed', { username: 'ada' }, null, null, 'Sign-in failed for ada'],
  ['auth.logout', { username: 'ada' }, 'ada', 1, 'ada signed out'],
  ['login_success', 'Login: ada from 10.0.0.5', null, null, 'Password accepted for ada'],
  ['login_failed', 'Failed login: ada from 10.0.0.5', null, null, 'Sign-in failed for ada'],
  ['mfa_enabled', 'MFA enabled', 'ada', 1, 'ada turned on two-step sign-in'],
  ['mfa_disabled', 'MFA disabled', 'ada', 1, 'ada turned off two-step sign-in'],
  ['mfa_disabled_admin', 'Admin force-disabled MFA for user bob', 'ada', 5, 'ada turned off two-step sign-in for user 5'],
  ['session_revoked_admin', 'Admin revoked session for user_id=5', 'ada', 9, 'ada ended a session of user 5'],
  ['print_file_uploaded', { target_printer_id: 3 }, 'ada', 12, 'ada uploaded print file 12 for Test X1C'],
  ['print_file.downloaded', { what: 'mesh' }, 'ada', 12, 'ada downloaded the 3D mesh of print file 12'],
  ['export.downloaded', { export: 'audit-logs', filters: { q: true } }, 'ada', null, 'ada exported the audit log'],
  ['backup.downloaded', { filename: 'odin-2026-11-13.db', bundle: false }, 'ada', null, 'ada downloaded backup odin-2026-11-13.db'],
  ['backup_created', { filename: 'odin-2026-11-13.db', size_bytes: 10 }, 'ada', null, 'ada created backup odin-2026-11-13.db'],
  ['backup_deleted', { filename: 'odin-2026-11-13.db' }, 'ada', null, 'ada deleted backup odin-2026-11-13.db'],
  ['timelapse.downloaded', null, 'ada', 4, 'ada downloaded timelapse 4'],
  ['timelapse.viewed', null, 'ada', 4, 'ada watched timelapse 4'],
  ['camera.viewed', { transport: 'webrtc' }, 'ada', 3, 'ada viewed the camera on Test X1C'],
  ['profile.exported', null, 'ada', 6, 'ada exported profile 6'],
  ['project.exported', null, 'ada', 6, 'ada exported project 6'],
  ['job.created', { item_name: 'Benchy', status: 'submitted' }, 'ada', 1, 'ada created job 1 (Benchy) and sent it for approval'],
  ['job.dispatched', { printer_id: 3 }, 'newowner', 1, 'newowner sent job 1 to Test X1C'],
  ['job.dispatch_requested', { printer_id: 8 }, 'newowner', 1, 'newowner asked to send job 1 to printer 8'],
  ['job.cancelled', { actor_user_id: 2, actor_token_id: null }, 'ada', 1, 'ada cancelled job 1'],
  ['job.approved', { approved_by: 2 }, 'ada', 1, 'ada approved job 1'],
  ['job.rejected', { rejected_by: 2, reason: 'wrong material' }, 'ada', 1, 'ada rejected job 1: wrong material'],
  ['job.deleted', { item_name: 'Benchy' }, 'ada', 1, 'ada deleted job 1 (Benchy)'],
  ['printer.created', { name: 'Test X1C', api_type: 'bambu' }, 'ada', 3, 'ada added printer Test X1C'],
  ['printer.updated', { fields: ['name', 'access_code'] }, 'ada', 3, 'ada edited Test X1C: name, access_code'],
  ['printer.deleted', { name: 'Test X1C', pinned_job_count: 0 }, 'ada', 3, 'ada removed printer Test X1C'],
  ['config.updated', { keys: ['spoolman_url', 'blackout_start'] }, 'ada', null, 'ada changed settings: spoolman_url, blackout_start'],
  ['branding.updated', { fields: ['app_name'] }, 'ada', null, 'ada changed branding: app_name'],
  ['oidc_config_updated', { fields: ['client_id', 'client_secret'] }, 'ada', null, 'ada changed single sign-on settings: client_id, client_secret'],
  ['smtp_config_updated', { enabled: true, host: 'mail.owned.invalid' }, 'ada', null, 'ada changed email (SMTP) settings'],
  ['ip_allowlist_updated', 'Enabled=True, 2 CIDRs', 'ada', null, 'ada changed the IP allowlist'],
  ['permissions_updated', { page_keys: ['audit'], action_keys: [] }, 'ada', null, 'ada changed role permissions'],
  ['education_mode_toggled', 'Education mode enabled', 'ada', null, 'ada turned Education mode on'],
  ['retention_updated', "Retention config: {'audit_logs_days': 365}", 'ada', null, "ada changed data retention to {'audit_logs_days': 365}"],
  ['user.created', { username: 'bob', role: 'operator' }, 'ada', 5, 'ada created user bob as operator'],
  ['user.deleted', { deleted_username: 'bob', actor_user_id: 1 }, 'ada', 5, 'ada deleted user bob'],
  ['user.role_changed', { new_role: 'admin', actor_user_id: 1 }, 'ada', 5, 'ada changed the role of user 5 to admin'],
  ['user.password_changed', { actor_user_id: 1, target_user_id: 5 }, 'ada', 5, 'ada changed the password of user 5'],
  ['user.password_reset', { target_user_id: 5 }, null, 5, 'The password of user 5 was reset by email link'],
  ['api_token_created', "Token 'ci' created", 'ada', 2, 'ada created API token ci'],
  ['api_token_revoked', "Token 'ci' revoked", 'ada', 2, 'ada revoked API token ci'],
  ['license.uploaded', { tier: 'pro', licensee: 'Owned Farm' }, 'ada', null, 'ada installed a pro license for Owned Farm'],
  ['license.activated', { tier: 'enterprise' }, 'ada', null, 'ada activated an enterprise license'],
  ['license.reactivated', { tier: 'pro', licensee: 'Owned Farm' }, 'ada', null, 'ada reactivated a pro license for Owned Farm'],
  ['license.removed', 'License removed, reverted to community tier', 'ada', null, 'ada removed the license'],
  ['license.unactivated', { key: 'ABCD...' }, 'ada', null, 'ada deactivated the license'],
]

test.each(CASES)('%s', (action, details, actor, entityId, sentence) => {
  expect(auditSentence(action, details, actor, { entityId, printerName })).toBe(sentence)
})

test('every action in the table has a case', () => {
  expect(CASES.map(c => c[0]).sort()).toEqual(Object.keys(SENTENCES).sort())
})

describe('fallback and edges', () => {
  test.each(['create', 'square.stock_baseline', '', 'constructor', 'toString', null, undefined, 7])('unknown action %s has no sentence', action => {
    expect(auditSentence(action, { name: 'x' }, 'ada')).toBeNull()
  })
  test('missing actor, details and names still read as a sentence', () => {
    expect(auditSentence('job.dispatched', null, null)).toBe('Someone sent a job to a printer')
    expect(auditSentence('job.dispatched', { printer_id: 3 }, '  ', { entityId: 0 })).toBe('Someone sent job 0 to printer 3')
    expect(auditSentence('api_token_revoked', { reason: 'legacy_camera_token', user_id: 2 }, null, { entityId: 4 })).toBe('Someone revoked API token 4')
    expect(auditSentence('api_token_created', 'unexpected', 'ada')).toBe('ada created an API token')
  })
  test('automatic dispatch is the scheduler, not the person who queued the job', () => {
    expect(auditSentence('job.dispatched', { printer_id: 3, trigger: 'scheduler', queued_by_user_id: 2 }, null, { entityId: 1, printerName }))
      .toBe('The scheduler sent job 1 to Test X1C')
  })
  test('single sign-on and erased accounts', () => {
    expect(auditSentence('auth.login', { method: 'oidc', username: 'ada' }, null, { entityId: 1 })).toBe('ada signed in with single sign-on')
    expect(auditSentence('auth.login_failed', { method: 'oidc', reason: 'auth_failed', username: null }, null)).toBe('Sign-in failed for an unknown account (single sign-on)')
    expect(auditSentence('login_failed', 'Failed login: [deleted]', null)).toBe('Sign-in failed for a deleted account')
  })
  test('values that could be secret never reach the sentence', () => {
    const secret = 'owned-secret-value'
    for (const action of Object.keys(SENTENCES)) {
      const sentence = auditSentence(action, { password: secret, token: secret, key: secret, value: secret, host: secret, changes: { access_code: secret }, manifest: secret, filters: { q: secret } }, 'ada', { entityId: 1 })
      expect(sentence).not.toContain(secret)
    }
  })
})
