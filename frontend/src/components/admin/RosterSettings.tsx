import { useEffect, useState } from 'react'
import { roster, type RosterConnection } from '../../api/roster'

const skipReasons: Record<string, string> = {
  roster_erased_subject: 'Previously erased account',
  roster_email_missing_or_invalid: 'Missing or invalid email address',
  roster_domain_refused: 'Email domain is not allowed',
  roster_role_unmatched: 'No matching class role',
  roster_link_declined: 'Existing-account link declined',
}
const common = [['client_id', 'Client ID'], ['public_origin', 'ODIN public HTTPS origin'], ['allowed_domains', 'Allowed email domains (comma separated)']]
const google = [['client_secret', 'Google OAuth client secret']]
const canvas = [['issuer', 'Canvas issuer'], ['deployment_id', 'Deployment ID'], ['authorization_url', 'OIDC authorization URL'],
  ['token_url', 'OAuth token URL'], ['jwks_url', 'Canvas public JWKS URL'], ['api_origin', 'Canvas API HTTPS origin'],
  ['key_id', 'Tool key ID'], ['private_key', 'Tool RSA private key (PEM)']]

export default function RosterSettings() {
  const [connections, setConnections] = useState<RosterConnection[]>([])
  const [provider, setProvider] = useState<'google' | 'canvas'>('google')
  const [config, setConfig] = useState<Record<string, string>>({})
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [courses, setCourses] = useState<{ id: string; title: string }[]>([])
  const [course, setCourse] = useState('')
  const [preview, setPreview] = useState<any>(null)
  const [decisions, setDecisions] = useState<Record<string, 'confirm' | 'decline'>>({})
  useEffect(() => { setDecisions({}) }, [preview])
  const connection = connections.find(c => c.provider === provider)
  useEffect(() => { roster.settings().then(r => setConnections(r.items)).catch(e => setError(e.message)) }, [])
  useEffect(() => {
    setConfig(Object.fromEntries(Object.entries(connection?.config || {}).map(([k, v]) => [k, Array.isArray(v) ? v.join(', ') : v])))
    setEnabled(connection?.enabled || false); setPreview(null); setCourses([]); setCourse('')
  }, [connection, provider])
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('')
    try { await action() } catch (e: any) { setError(e.message || 'Roster operation failed') } finally { setBusy(false) }
  }
  const fields = [...common, ...(provider === 'google' ? google : canvas)]
  const base = connection ? `${connection.config.public_origin}/api/auth/classroom/${connection.id}` : ''
  return <section className="mt-6 space-y-4" aria-label="Roster sync and classroom sign-in">
    <h2 className="text-lg font-semibold">Roster sync and classroom sign-in</h2>
    <p>Disabled until a school administrator configures and enables a provider. Imports require a dry run and preserve class history.</p>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <label>Provider <select aria-label="Roster provider" value={provider} disabled={busy} onChange={e => setProvider(e.target.value as 'google' | 'canvas')}>
      <option value="google">Google Classroom</option><option value="canvas">Canvas LTI 1.3</option>
    </select></label>
    <form className="space-y-3" onSubmit={e => { e.preventDefault(); run(async () => {
      const values: Record<string, unknown> = { ...config, allowed_domains: (config.allowed_domains || '').split(',').map(v => v.trim().toLowerCase()).filter(Boolean) }
      for (const key of ['client_secret', 'private_key']) if (!values[key]) delete values[key]
      const saved = await roster.save({ provider, enabled, revision: connection?.revision || 0, config: values })
      setConnections(old => [...old.filter(c => c.provider !== provider), saved]); setPreview(null)
      setMessage('Settings saved. Authorization is retained unless provider credentials change.')
    }) }}>
      {fields.map(([key, label]) => <label key={key} className="block">{label}
        {key === 'private_key' ? <textarea aria-label={label} value={config[key] || ''} autoComplete="off" rows={4}
          className="block w-full bg-[var(--brand-input-bg)] border rounded p-2" onChange={e => setConfig({ ...config, [key]: e.target.value })} /> :
          <input aria-label={label} type={key === 'client_secret' ? 'password' : 'text'} autoComplete="off" value={config[key] || ''}
            className="block w-full bg-[var(--brand-input-bg)] border rounded p-2" onChange={e => setConfig({ ...config, [key]: e.target.value })} />}
      </label>)}
      <p>Leave a saved secret blank to retain it. Credentials are encrypted and never returned.</p>
      <label className="block"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} /> Enable roster sync and classroom sign-in</label>
      <button disabled={busy} type="submit" className="border rounded p-2">Save roster settings</button>
    </form>
    {connection && <div className="space-y-3 break-all">
      <p>Status: {connection.enabled ? 'Enabled' : 'Disabled'} · {connection.connected ? 'Provider authorized' : 'Provider authorization required'}</p>
      {provider === 'google' ? <>
        <p>Register this OAuth callback: <code>{base}/callback</code></p>
        <p>Student sign-in: <a className="underline" href={`${base}/login`}>{base}/login</a></p>
        <button disabled={busy || !connection.enabled} className="border rounded p-2" onClick={() => run(async () => {
          const r = await roster.connect(connection.id); window.location.assign(r.authorization_url)
        })}>Connect Google Classroom</button>
      </> : <>
        <p>Register the school’s Canvas developer key with these URLs. Launch in a new window so browser cookie restrictions do not prevent sign-in.</p>
        <p>OIDC initiation: <code>{base}/login</code></p><p>Redirect / target link: <code>{base}/launch</code></p>
        <p>Tool JWKS: <code>{base}/jwks</code></p>
        <p>A signed Canvas launch makes its course available below. The first launch does not create an ODIN account; import its roster before students sign in.</p>
      </>}
      <button disabled={busy || !connection.enabled} className="border rounded p-2" onClick={() => run(async () => {
        setPreview(null); const r = await roster.courses(connection.id); setCourses(r.items); setCourse(r.items[0]?.id || '')
        if (!r.items.length) setMessage('No courses available. Connect Google or launch a Canvas course first.')
      })}>Load provider courses</button>
      {courses.length > 0 && <div><label>Course <select value={course} onChange={e => { setCourse(e.target.value); setPreview(null) }}>
        {courses.map(c => <option value={c.id} key={c.id}>{c.title}</option>)}
      </select></label> <button disabled={busy} className="border rounded p-2" onClick={() => run(async () => {
        setPreview(null); setPreview(await roster.preview(connection.id, course))
      })}>Dry run roster import</button></div>}
      {preview && <div className="border rounded p-3 space-y-2">
        <h3>{preview.course.title}</h3>
        <p>Create {preview.create_users} accounts; add/change {preview.add_memberships.length} memberships; revoke {preview.revoke_memberships.length} memberships.</p>
        <p>{preview.class_id ? 'Update existing class.' : 'Create a new class and dedicated cost center.'} History is retained. Preview expires in 10 minutes.</p>
        <ul>{preview.add_memberships.map((m: any) => <li key={m.subject + m.role}>Add {m.email} as {m.role}</li>)}
          {preview.revoke_memberships.map((m: any) => <li key={m.user_id + m.role}>Revoke user #{m.user_id}: {m.role}</li>)}</ul>
        {(preview.account_links || []).map((link: any) => <div key={link.subject}>
          <p>{link.label} · ODIN username: {link.username} · Provider email: {link.email} · Class role: {(link.roles || []).join(', ')}</p>
          <label>Link decision for {link.username}
            <select aria-label={`Link decision for ${link.username}`} value={decisions[link.subject] || ''} disabled={busy}
              onChange={e => setDecisions(old => ({ ...old, [link.subject]: e.target.value as 'confirm' | 'decline' }))}>
              <option value="">Choose confirm or decline</option><option value="confirm">Confirm link</option><option value="decline">Decline and skip</option>
            </select>
          </label>
          {decisions[link.subject] === 'decline' && <p>Skipped: {link.email} — existing-account link declined.</p>}
        </div>)}
        {(preview.skipped || []).length > 0 && <div><p>Skipped {preview.skipped.length} roster entries:</p>
          <p>Clearing an erased-account suppression allows that entry to return in a new dry run.</p>
          <ul>{preview.skipped.map((item: any, index: number) => <li key={index}>
            {skipReasons[item.reason] || 'Roster entry unavailable'}
            {item.reason === 'roster_erased_subject' && item.clear_handle && <button disabled={busy}
              className="border rounded p-2 ml-2" aria-label={`Clear suppression for skipped entry ${index + 1}`}
              onClick={() => run(async () => {
                await roster.clearTombstone(connection.id, preview.preview_token, item.clear_handle)
                setPreview(null); setMessage('Erasure suppression cleared. Run a new dry run before importing.')
              })}>Clear suppression</button>}
          </li>)}</ul></div>}
        <button disabled={busy || (preview.account_links || []).some((link: any) => !decisions[link.subject])} className="border rounded p-2" onClick={() => run(async () => {
          const links = (preview.account_links || []).map(({ subject, user_id, mode }: any) => ({ subject, user_id, mode, decision: decisions[subject] }))
          const result = await roster.apply(connection.id, preview.preview_token, links)
          setPreview(null); setMessage('Roster imported. The class is available in Education.' +
            (result.skipped?.length ? ` Skipped ${result.skipped.length}: ${result.skipped.map(item => skipReasons[item.reason] || 'Roster entry unavailable').join('; ')}.` : ''))
        })}>Confirm reviewed import</button>
      </div>}
    </div>}
  </section>
}
