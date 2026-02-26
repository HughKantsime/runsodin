import { useState, useEffect, useRef } from 'react'
import SSOButton from '../../components/SSOButton'
import { useNavigate } from 'react-router-dom'
import { Lock, User, AlertCircle, ShieldCheck, Loader2 } from 'lucide-react'
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
  const [oidcLoading, setOidcLoading] = useState(false)

  // MFA state
  const [mfaRequired, setMfaRequired] = useState(false)
  const [mfaToken, setMfaToken] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  const mfaInputRef = useRef(null)

  // Forgot password state
  const [showForgot, setShowForgot] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [forgotSent, setForgotSent] = useState(false)
  const [forgotLoading, setForgotLoading] = useState(false)
  const [smtpEnabled, setSmtpEnabled] = useState(false)

  useEffect(() => {
    api.auth.capabilities().then(c => setSmtpEnabled(c?.smtp_enabled || false)).catch(() => {})
  }, [])


  // Handle OIDC callback parameters
  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const oidcCode = urlParams.get('oidc_code');
    const urlError = urlParams.get('error');

    if (oidcCode) {
      setOidcLoading(true);
      window.history.replaceState({}, '', '/');
      api.auth.oidcExchange(oidcCode)
        .then(() => { window.location.href = '/'; })
        .catch(() => { setError('SSO login failed. Please try again.'); setOidcLoading(false); });
    }

    if (urlError) {
      setError(urlError === 'user_not_found'
        ? 'Your account is not authorized. Contact an administrator.'
        : 'SSO login failed. Please try again.');
      window.history.replaceState({}, '', '/login');
    }
  }, []);

  // Auto-focus MFA input
  useEffect(() => {
    if (mfaRequired && mfaInputRef.current) {
      mfaInputRef.current.focus()
    }
  }, [mfaRequired])

  const completeLogin = () => {
    // Session is established via httpOnly cookie set by the server.
    // No localStorage token storage needed.
  }

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

      // Check if MFA is required
      if (data.mfa_required) {
        setMfaToken(data.access_token)
        setMfaRequired(true)
        setIsLoading(false)
        return
      }

      completeLogin(data)
      await refreshPermissions()
      navigate('/')
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  const handleMfaSubmit = async (e) => {
    if (e) e.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      const response = await fetch('/api/auth/mfa/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mfa_token: mfaToken, code: mfaCode })
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail || 'Invalid code')
      }

      const data = await response.json()
      completeLogin(data)
      await refreshPermissions()
      navigate('/')
    } catch (err) {
      setError(err.message)
      setMfaCode('')
    } finally {
      setIsLoading(false)
    }
  }

  // Auto-submit MFA when 6 digits entered
  const handleMfaCodeChange = (e) => {
    const val = e.target.value.replace(/\D/g, '')
    setMfaCode(val)
    if (val.length === 6) {
      // Defer to next tick so state is updated
      setTimeout(() => handleMfaSubmit(), 0)
    }
  }

  if (oidcLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4"
        style={{ backgroundColor: 'var(--brand-content-bg)' }}>
        <div className="text-center">
          <Loader2 size={32} className="animate-spin mx-auto mb-4" style={{ color: 'var(--brand-accent)' }} />
          <p style={{ color: 'var(--brand-text-secondary)' }}>Completing sign-in...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4"
      style={{ backgroundColor: 'var(--brand-content-bg)' }}>
      <div className="w-full max-w-md">
        <div className="rounded-lg p-8"
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
              {mfaRequired ? 'Two-Factor Authentication' : branding.app_subtitle}
            </p>
          </div>

          {/* Error */}
          {error && (
            <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
              <AlertCircle size={20} className="text-red-400" />
              <span className="text-red-400">{error}</span>
            </div>
          )}

          {mfaRequired ? (
            /* MFA Code Form */
            <form onSubmit={handleMfaSubmit} className="space-y-6">
              <div className="text-center mb-4">
                <ShieldCheck size={40} className="mx-auto mb-3" style={{ color: 'var(--brand-accent)' }} />
                <p className="text-sm" style={{ color: 'var(--brand-text-secondary)' }}>
                  Enter the 6-digit code from your authenticator app
                </p>
              </div>
              <div>
                <div className="relative">
                  <Lock size={18} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--brand-text-muted)' }} />
                  <input
                    ref={mfaInputRef}
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    value={mfaCode}
                    onChange={handleMfaCodeChange}
                    className="w-full rounded-lg py-3 pl-10 pr-4 focus:outline-none text-center text-2xl tracking-[0.5em] font-mono"
                    style={{
                      backgroundColor: 'var(--brand-input-bg)',
                      border: '1px solid var(--brand-input-border)',
                      color: 'var(--brand-text-primary)',
                    }}
                    placeholder="000000"
                    required
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={isLoading || mfaCode.length !== 6}
                className="w-full font-medium py-3 rounded-lg transition-colors disabled:opacity-50"
                style={{ backgroundColor: 'var(--brand-primary)', color: '#fff' }}
              >
                {isLoading ? 'Verifying...' : 'Verify'}
              </button>

              <button
                type="button"
                onClick={() => { setMfaRequired(false); setMfaToken(''); setMfaCode(''); setError('') }}
                className="w-full text-sm py-2 transition-colors"
                style={{ color: 'var(--brand-text-muted)' }}
              >
                Back to login
              </button>
            </form>
          ) : (
            /* Normal Login Form */
            <>
              <form onSubmit={handleSubmit} className="space-y-6">
                <div>
                  <label className="block text-sm mb-2" style={{ color: 'var(--brand-text-secondary)' }}>Username</label>
                  <div className="relative">
                    <User size={18} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--brand-text-muted)' }} />
                    <input
                      type="text"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      className="w-full rounded-lg py-3 pl-10 pr-4 focus:outline-none"
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
                  <label className="block text-sm mb-2" style={{ color: 'var(--brand-text-secondary)' }}>Password</label>
                  <div className="relative">
                    <Lock size={18} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--brand-text-muted)' }} />
                    <input
                      type="password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full rounded-lg py-3 pl-10 pr-4 focus:outline-none"
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
                  className="w-full font-medium py-3 rounded-lg transition-colors disabled:opacity-50"
                  style={{ backgroundColor: 'var(--brand-primary)', color: '#fff' }}
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
                          type="email"
                          value={forgotEmail}
                          onChange={(e) => setForgotEmail(e.target.value)}
                          placeholder="you@example.com"
                          className="w-full px-3 py-2 rounded-lg text-sm"
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
                            className="flex-1 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
                            style={{ backgroundColor: 'var(--brand-primary)', color: '#fff' }}
                          >
                            {forgotLoading ? 'Sending...' : 'Send Reset Link'}
                          </button>
                          <button onClick={() => setShowForgot(false)} className="px-3 py-2 rounded-lg text-sm" style={{ color: 'var(--brand-text-muted)' }}>
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
          <p className="text-[10px] mt-1" style={{ opacity: 0.6 }}>Powered by O.D.I.N.</p>
        </div>
      </div>
    </div>
  )
}
