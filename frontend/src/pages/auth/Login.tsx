import { useState, useEffect } from 'react'
import LoginContinuation, { readLoginAuthority, verifyLoginSession, type LoginAuthority } from '../../components/auth/LoginContinuation'
import SSOButton from '../../components/auth/SSOButton'
import { useNavigate } from 'react-router-dom'
import { Lock, User, AlertCircle } from 'lucide-react'
import { useBranding } from '../../BrandingContext'
import { refreshPermissions } from '../../permissions'
import * as api from '../../api'

export default function Login() {
  const navigate = useNavigate()
  const branding = useBranding()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)

  const [authority, setAuthority] = useState<LoginAuthority | null>(null)

  // Forgot password state
  const [showForgot, setShowForgot] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [forgotSent, setForgotSent] = useState(false)
  const [forgotLoading, setForgotLoading] = useState(false)
  const [smtpEnabled, setSmtpEnabled] = useState(false)

  useEffect(() => {
    api.auth.capabilities().then(c => setSmtpEnabled(c?.smtp_enabled || false)).catch(() => {})
  }, [])


const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ username, password })
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail || 'Login failed')
      }

      const data = await response.json()

      const continuation = readLoginAuthority(data)
      setPassword('')
      if (continuation) { setAuthority(continuation); return }
      if (typeof data.access_token !== 'string' || data.token_type !== 'bearer') throw new Error('Invalid sign-in response')
      await verifyLoginSession(data.access_token)
      await refreshPermissions()
      navigate('/')
    } catch (err: any) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-4"
      style={{ backgroundColor: 'var(--brand-content-bg)' }}>
      <div className="w-full max-w-md">
        <div className="rounded-md p-8"
          style={{
            backgroundColor: 'var(--brand-card-bg)',
            border: '1px solid var(--brand-card-border)',
          }}>
          {/* Logo */}
          <div className="text-center mb-8">
            {branding.logo_url ? (
              <img src={branding.logo_url} alt={branding.app_name} className="h-12 mx-auto mb-2" />
            ) : (
              <h1 className="text-3xl font-display font-bold" style={{ color: 'var(--brand-accent)' }}>
                {branding.app_name.toUpperCase()}
              </h1>
            )}
            <p className="mt-1" style={{ color: 'var(--brand-text-muted)' }}>
              {authority ? 'Two-Factor Authentication' : branding.app_subtitle}
            </p>
          </div>

          {/* Error */}
          {error && (
            <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-md flex items-center gap-3">
              <AlertCircle size={20} className="text-red-400" />
              <span className="text-red-400">{error}</span>
            </div>
          )}

          {authority ? (
            <LoginContinuation authority={authority}
              onComplete={async () => { await refreshPermissions(); navigate('/') }}
              onRestart={() => { setAuthority(null); setError('') }} />
          ) : (
            /* Normal Login Form */
            <>
              <form onSubmit={handleSubmit} className="space-y-6">
                <div>
                  <label htmlFor="login-username" className="block text-sm mb-2" style={{ color: 'var(--brand-text-secondary)' }}>Username</label>
                  <div className="relative">
                    <User size={18} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--brand-text-muted)' }} />
                    <input
                      id="login-username"
                      name="username"
                      type="text"
                      autoComplete="username"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      className="w-full rounded-md py-3 pl-10 pr-4 focus:outline-hidden"
                      style={{
                        backgroundColor: 'var(--brand-input-bg)',
                        border: '1px solid var(--brand-input-border)',
                        color: 'var(--brand-text-primary)',
                      }}
                      placeholder="Enter username"
                      required
                    />
                  </div>
                </div>

                <div>
                  <label htmlFor="login-password" className="block text-sm mb-2" style={{ color: 'var(--brand-text-secondary)' }}>Password</label>
                  <div className="relative">
                    <Lock size={18} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--brand-text-muted)' }} />
                    <input
                      id="login-password"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full rounded-md py-3 pl-10 pr-4 focus:outline-hidden"
                      style={{
                        backgroundColor: 'var(--brand-input-bg)',
                        border: '1px solid var(--brand-input-border)',
                        color: 'var(--brand-text-primary)',
                      }}
                      placeholder="Enter password"
                      required
                    />
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={isLoading}
                  className="w-full font-medium py-3 rounded-md transition-colors disabled:opacity-50"
                  style={{ backgroundColor: 'var(--brand-primary)', color: 'var(--brand-on-primary)' }}
                >
                  {isLoading ? 'Signing in...' : 'Sign In'}
                </button>
              </form>
              <div className="flex items-center gap-3 my-6">
                <div className="flex-1 border-t" style={{ borderColor: 'var(--brand-input-border)' }} />
                <span className="text-xs" style={{ color: 'var(--brand-text-muted)' }}>or</span>
                <div className="flex-1 border-t" style={{ borderColor: 'var(--brand-input-border)' }} />
              </div>
              <SSOButton />
              <div className="text-center text-xs mt-6" style={{ color: 'var(--brand-text-muted)' }}>
                {smtpEnabled ? (
                  showForgot ? (
                    forgotSent ? (
                      <p className="text-green-400">If that email is registered, a reset link has been sent.</p>
                    ) : (
                      <div className="space-y-2">
                        <p>Enter your email to receive a reset link</p>
                        <input
                          aria-label="Account email"
                          type="email"
                          value={forgotEmail}
                          onChange={(e) => setForgotEmail(e.target.value)}
                          placeholder="you@example.com"
                          className="w-full px-3 py-2 rounded-md text-sm"
                          style={{ backgroundColor: 'var(--brand-input-bg)', border: '1px solid var(--brand-input-border)', color: 'var(--brand-text-primary)' }}
                        />
                        <div className="flex gap-2">
                          <button
                            onClick={async () => {
                              if (!forgotEmail) return
                              setForgotLoading(true)
                              try { await api.auth.forgotPassword(forgotEmail); setForgotSent(true) }
                              catch { setForgotSent(true) }
                              setForgotLoading(false)
                            }}
                            disabled={forgotLoading || !forgotEmail}
                            className="flex-1 py-2 rounded-md text-sm font-medium disabled:opacity-50"
                            style={{ backgroundColor: 'var(--brand-primary)', color: 'var(--brand-on-primary)' }}
                          >
                            {forgotLoading ? 'Sending...' : 'Send Reset Link'}
                          </button>
                          <button onClick={() => setShowForgot(false)} className="px-3 py-2 rounded-md text-sm" style={{ color: 'var(--brand-text-muted)' }}>
                            Cancel
                          </button>
                        </div>
                      </div>
                    )
                  ) : (
                    <button onClick={() => setShowForgot(true)} className="hover:underline" style={{ color: 'var(--brand-text-muted)' }}>
                      Forgot your password?
                    </button>
                  )
                ) : (
                  <p>Forgot your password? Contact your administrator.</p>
                )}
              </div>
            </>
          )}
        </div>

        <div className="text-center mt-6" style={{ color: 'var(--brand-text-muted)' }}>
          <p className="text-xs">v{__APP_VERSION__}</p>
          <p className="text-[10px] mt-1">Powered by O.D.I.N.</p>
        </div>
      </div>
    </main>
  )
}
