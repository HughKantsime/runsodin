import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'

export const oidcNavigation = { finish: () => window.location.replace('/') }

function readCallback() {
  const { pathname, search } = window.location
  const params = new URLSearchParams(search)
  if (!['/', '/login'].includes(pathname) ||
      (!params.has('oidc_code') && !params.has('error'))) return null
  const codes = params.getAll('oidc_code')
  return { code: !params.has('error') && codes.length === 1 ? codes[0].trim() : '' }
}

async function establishSession(code: string) {
  if (!code) throw new Error('Invalid callback')
  const options = { credentials: 'include', cache: 'no-store', redirect: 'error' } as const
  const exchange = await fetch('/api/auth/oidc/exchange', {
    ...options, method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  })
  if (!exchange.ok || exchange.redirected) throw new Error('Exchange failed')
  const session = await fetch('/api/auth/me', options)
  if (!session.ok || session.redirected) throw new Error('Session unavailable')
  const user = await session.json()
  if (typeof user?.username !== 'string' || !user.username.trim() ||
      typeof user?.role !== 'string' || !user.role.trim()) throw new Error('Invalid session')
}

// Must wrap all providers: none may issue unauthenticated requests mid-callback.
export default function OIDCBootstrap({ children }: { children: ReactNode }) {
  const [callback] = useState(readCallback)
  const [failed, setFailed] = useState(false)
  const operation = useRef<Promise<void> | null>(null)

  useEffect(() => {
    if (!callback) return
    let active = true
    window.history.replaceState(null, '', '/login')
    // StrictMode replays effects; keep the entire one-time exchange operation.
    operation.current ??= establishSession(callback.code)
    operation.current.then(() => {
      if (active) oidcNavigation.finish()
    }).catch(() => { if (active) setFailed(true) })
    return () => { active = false }
  }, [callback])

  if (!callback) return children
  return (
    <main className="min-h-screen flex items-center justify-center p-4"
      style={{ backgroundColor: 'var(--brand-content-bg)' }}>
      <div className="text-center max-w-md">
        <h1 className="mb-4" style={{ color: 'var(--brand-accent)' }}>O.D.I.N.</h1>
        {failed ? <div role="alert">
          <p>SSO sign-in could not be completed. Please try again or contact your administrator.</p>
          <a href="/login" className="inline-block mt-4 underline"
            style={{ color: 'var(--brand-accent)' }}>Return to sign in</a>
        </div> : <div role="status">
          <Loader2 aria-hidden="true" size={32} className="animate-spin mx-auto mb-4"
            style={{ color: 'var(--brand-accent)' }} />
          <p>Completing sign-in...</p>
        </div>}
      </div>
    </main>
  )
}
