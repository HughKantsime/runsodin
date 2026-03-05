import { useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { Check, Circle } from 'lucide-react'
import { auth } from '../../api'
import toast from 'react-hot-toast'

export default function ResetPassword() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const token = searchParams.get('token') || ''

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)

  const checks = {
    length: password.length >= 8,
    upper: /[A-Z]/.test(password),
    lower: /[a-z]/.test(password),
    number: /\d/.test(password),
    match: password && password === confirm,
  }
  const valid = Object.values(checks).every(Boolean)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!valid) return
    setLoading(true)
    try {
      await auth.resetPassword(token, password)
      setDone(true)
      toast.success('Password updated — please log in')
      setTimeout(() => navigate('/login'), 2000)
    } catch (err) {
      toast.error(err.message || 'Reset failed — link may be expired')
    } finally {
      setLoading(false)
    }
  }

  if (!token) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[var(--brand-content-bg)]">
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-8 max-w-sm w-full text-center">
          <p className="text-[var(--brand-text-secondary)]">Invalid or missing reset token.</p>
          <a href="/login" className="text-[var(--brand-primary)] hover:underline text-sm mt-2 block">Back to login</a>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-[var(--brand-content-bg)]">
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-8 max-w-sm w-full">
        <h1 className="text-xl font-semibold mb-6 text-center" style={{ color: 'var(--brand-text-primary)' }}>Reset Password</h1>

        {done ? (
          <div className="text-center">
            <p className="text-green-400 mb-2">Password updated successfully.</p>
            <p className="text-[var(--brand-text-muted)] text-sm">Redirecting to login...</p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm text-[var(--brand-text-secondary)] mb-1">New Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm"
                style={{ color: 'var(--brand-text-primary)' }}
                autoFocus
              />
            </div>
            <div>
              <label className="block text-sm text-[var(--brand-text-secondary)] mb-1">Confirm Password</label>
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm"
                style={{ color: 'var(--brand-text-primary)' }}
              />
            </div>

            <div className="space-y-1 text-xs">
              {[
                ['length', '8+ characters'],
                ['upper', 'Uppercase letter'],
                ['lower', 'Lowercase letter'],
                ['number', 'Number'],
                ['match', 'Passwords match'],
              ].map(([key, label]) => (
                <div key={key} className={`flex items-center gap-1.5 ${checks[key] ? 'text-green-400' : 'text-[var(--brand-text-muted)]'}`}>
                  {checks[key] ? <Check size={12} /> : <Circle size={12} />} {label}
                </div>
              ))}
            </div>

            <button
              type="submit"
              disabled={!valid || loading}
              className="w-full py-2 rounded-md text-white text-sm font-medium disabled:opacity-50 transition-colors"
              style={{ backgroundColor: 'var(--brand-primary)' }}
            >
              {loading ? 'Resetting...' : 'Reset Password'}
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
