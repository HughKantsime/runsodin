import { useState, useEffect } from 'react'
import SSOButton from '../components/SSOButton'
import { useNavigate } from 'react-router-dom'
import { Lock, User, AlertCircle } from 'lucide-react'
import { useBranding } from '../BrandingContext'
import { refreshPermissions } from '../permissions'

export default function Login() {
  const navigate = useNavigate()
  const branding = useBranding()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)

  
  // Handle OIDC callback
  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    const urlError = urlParams.get('error');
    
    if (urlToken) {
      localStorage.setItem('token', urlToken);
      window.history.replaceState({}, '', '/');
      window.location.reload();
    }
    
    if (urlError) {
      setError(urlError === 'user_not_found' 
        ? 'Your account is not authorized. Contact an administrator.'
        : 'SSO login failed. Please try again.');
      window.history.replaceState({}, '', '/login');
    }
  }, []);

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
      // Store token
      localStorage.setItem('token', data.access_token)
      const payload = JSON.parse(atob(data.access_token.split('.')[1]));
      localStorage.setItem('user', JSON.stringify({
        username: payload.sub,
        role: payload.role
      }))

      // Fetch and cache RBAC permissions
      await refreshPermissions()

      // Redirect to dashboard
      navigate('/')
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4"
      style={{ backgroundColor: 'var(--brand-content-bg)' }}>
      <div className="w-full max-w-md">
        <div className="rounded p-8"
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
              {branding.app_subtitle}
            </p>
          </div>

          {/* Error */}
          {error && (
            <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
              <AlertCircle size={20} className="text-red-400" />
              <span className="text-red-400">{error}</span>
            </div>
          )}

          {/* Form */}
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
              <SSOButton />
        </div>

        <p className="text-center text-sm mt-6" style={{ color: 'var(--brand-text-muted)' }}>
          {branding.app_name} {branding.app_subtitle} v{__APP_VERSION__}
        </p>
      </div>
    </div>
  )
}
