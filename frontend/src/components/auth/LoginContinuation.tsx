import { useEffect, useRef, useState } from 'react'

export type LoginAuthority = { kind: 'challenge' | 'enrollment'; token: string }
export function readLoginAuthority(data: unknown): LoginAuthority | null {
  const value = data as Record<string, unknown> | null
  if (!value || typeof value !== 'object') throw new Error('Invalid sign-in response')
  for (const flag of ['mfa_required', 'mfa_setup_required']) {
    if (value[flag] !== undefined && typeof value[flag] !== 'boolean') throw new Error('Invalid sign-in response')
  }
  const challenge = value.mfa_required === true
  const enrollment = value.mfa_setup_required === true
  if (challenge && enrollment) throw new Error('Invalid sign-in response')
  if (!challenge && !enrollment) return null
  if (typeof value.access_token !== 'string' || !value.access_token.trim()) throw new Error('Invalid sign-in response')
  return { kind: enrollment ? 'enrollment' : 'challenge', token: value.access_token }
}

export async function verifyLoginSession(token?: string) {
  let expected: { sub: string; role: string } | null = null
  if (token !== undefined) {
    try {
      const parts = token.split('.')
      if (parts.length !== 3) throw new Error()
      const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
      if (typeof payload.sub !== 'string' || !payload.sub.trim() || typeof payload.role !== 'string' ||
          !payload.role.trim() || payload.mfa_pending || payload.mfa_setup_required || payload.ws ||
          typeof payload.exp !== 'number' || !Number.isFinite(payload.exp) || payload.exp * 1000 <= Date.now()) throw new Error()
      expected = payload
    } catch { throw new Error('Invalid sign-in response') }
  }
  const response = await fetch('/api/auth/me', { credentials: 'include', cache: 'no-store', redirect: 'error' })
  if (!response.ok || response.redirected) throw new Error('Session unavailable')
  const user = await response.json()
  if (typeof user?.username !== 'string' || !user.username.trim() ||
      typeof user?.role !== 'string' || !user.role.trim() ||
      (expected && (user.username !== expected.sub || user.role !== expected.role))) throw new Error('Invalid session')
}

type Setup = { qr_code: string; secret: string; expires_at: number }
export default function LoginContinuation({ authority, onComplete, onRestart }: {
  authority: LoginAuthority; onComplete: () => Promise<void> | void; onRestart: () => void
}) {
  const [setup, setSetup] = useState<Setup | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [restart, setRestart] = useState(false)
  const operation = useRef<Promise<Setup> | null>(null)
  const pending = useRef(false)
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])
  useEffect(() => {
    if (authority.kind !== 'enrollment') return
    let active = true
    operation.current ??= fetch('/api/auth/mfa/enrollment/setup', {
      method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: { Authorization: `Bearer ${authority.token}` },
    }).then(async response => {
      if (!response.ok || response.redirected) throw new Error('Setup unavailable')
      const data = await response.json()
      if (typeof data.qr_code !== 'string' || !data.qr_code.startsWith('data:image/png;base64,') ||
          typeof data.secret !== 'string' || !/^[A-Z2-7]{32}$/.test(data.secret) ||
          typeof data.expires_at !== 'number' || !Number.isFinite(data.expires_at) || data.expires_at * 1000 <= Date.now()) {
        throw new Error('Setup unavailable')
      }
      return data as Setup
    })
    operation.current.then(data => { if (active) setSetup(data) }).catch(() => {
      if (active) { setRestart(true); setError('Authenticator setup could not be confirmed. Restart sign-in.') }
    })
    return () => { active = false }
  }, [authority])
  useEffect(() => {
    if (!setup) return
    const expire = () => { setSetup(null); setCode(''); setRestart(true); setError('Setup expired. Restart sign-in.') }
    const timer = window.setTimeout(expire, Math.max(0, Math.min(setup.expires_at * 1000 - Date.now(), 2147483647)))
    return () => window.clearTimeout(timer)
  }, [setup])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (pending.current || restart || !/^[0-9]{6}$/.test(code) || (authority.kind === 'enrollment' && !setup)) return
    pending.current = true; setBusy(true); setError('')
    try {
      const enrollment = authority.kind === 'enrollment'
      const response = await fetch(enrollment ? '/api/auth/mfa/enrollment/confirm' : '/api/auth/mfa/verify', {
        method: 'POST', credentials: 'include', cache: 'no-store', redirect: 'error',
        headers: { 'Content-Type': 'application/json', ...(enrollment ? { Authorization: `Bearer ${authority.token}` } : {}) },
        body: JSON.stringify(enrollment ? { code } : { mfa_token: authority.token, code }),
      })
      if (!live.current) return
      if (!response.ok || response.redirected) {
        if (response.status === 400 && !response.redirected) {
          setError('That code was not accepted. Check your authenticator and try again.'); setCode(''); return
        }
        throw new Error('Verification unconfirmed')
      }
      const data = await response.json()
      if (typeof data?.access_token !== 'string' || data.token_type !== 'bearer' || readLoginAuthority(data)) throw new Error('Invalid sign-in response')
      await verifyLoginSession(data.access_token)
      if (live.current) await onComplete()
    } catch {
      if (live.current) { setSetup(null); setCode(''); setRestart(true); setError('Sign-in could not be confirmed. Restart sign-in.') }
    } finally {
      pending.current = false
      if (live.current) setBusy(false)
    }
  }
  async function cancel() {
    if (pending.current) return
    pending.current = true; setBusy(true)
    if (authority.kind === 'enrollment' && !restart) {
      const controller = new AbortController()
      const timer = window.setTimeout(() => controller.abort(), 3000)
      // Navigation is immediate; cancellation remains best effort and never implies completion.
      void fetch('/api/auth/mfa/enrollment/cancel', { method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal, headers: { Authorization: `Bearer ${authority.token}` } })
        .catch(() => {}).finally(() => window.clearTimeout(timer))
    }
    if (live.current) onRestart()
  }
  return <section aria-label="Complete sign-in" className="space-y-4">
    <h2>{authority.kind === 'enrollment' ? 'Set up two-factor authentication' : 'Two-factor authentication'}</h2>
    {error && <p role="alert">{error}</p>}
    {!restart && <>
      {authority.kind === 'enrollment' && (setup ? <>
        <p>Scan this code with your authenticator app, then enter its six-digit code.</p>
        <img src={setup.qr_code} alt="Authenticator setup QR code" className="mx-auto w-48 h-48" />
        <details><summary>Enter a setup key instead</summary><code>{setup.secret}</code></details>
        <p>Setup expires at {new Date(setup.expires_at * 1000).toLocaleTimeString()}.</p>
      </> : <p role="status">Preparing authenticator setup...</p>)}
      <form onSubmit={submit} className="space-y-4">
        <label htmlFor="continuation-code">Authentication code</label>
        <input id="continuation-code" autoFocus inputMode="numeric" autoComplete="one-time-code" maxLength={6}
          value={code} disabled={busy || (authority.kind === 'enrollment' && !setup)}
          onChange={event => setCode(event.target.value.replace(/\D/g, ''))} className="w-full rounded-md p-3 text-black" />
        <button type="submit" disabled={busy || code.length !== 6 || (authority.kind === 'enrollment' && !setup)}
          className="w-full rounded-md p-3" style={{ backgroundColor: 'var(--brand-primary)', color: 'var(--brand-on-primary)' }}>
          {busy ? 'Verifying...' : 'Verify and sign in'}
        </button>
      </form>
    </>}
    <button type="button" disabled={busy} onClick={cancel} className="underline">{restart ? 'Restart sign-in' : 'Cancel and return to sign in'}</button>
  </section>
}
