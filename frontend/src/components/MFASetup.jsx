import { useState, useEffect } from 'react'
import { ShieldCheck, ShieldOff, Loader2, Copy, CheckCircle, AlertTriangle } from 'lucide-react'
import { auth } from '../api'

export default function MFASetup() {
  const [mfaEnabled, setMfaEnabled] = useState(false)
  const [loading, setLoading] = useState(true)
  const [setupData, setSetupData] = useState(null)
  const [confirmCode, setConfirmCode] = useState('')
  const [disableCode, setDisableCode] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [secretCopied, setSecretCopied] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)

  useEffect(() => {
    auth.mfaStatus().then(data => {
      setMfaEnabled(data.mfa_enabled)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  const handleSetup = async () => {
    setError('')
    setSuccess('')
    setActionLoading(true)
    try {
      const data = await auth.mfaSetup()
      setSetupData(data)
    } catch (err) {
      setError(err.message || 'Failed to start MFA setup')
    } finally {
      setActionLoading(false)
    }
  }

  const handleConfirm = async (e) => {
    e.preventDefault()
    setError('')
    setActionLoading(true)
    try {
      await auth.mfaConfirm(confirmCode)
      setMfaEnabled(true)
      setSetupData(null)
      setConfirmCode('')
      setSuccess('MFA enabled successfully')
    } catch (err) {
      setError(err.message || 'Invalid code')
    } finally {
      setActionLoading(false)
    }
  }

  const handleDisable = async (e) => {
    e.preventDefault()
    setError('')
    setActionLoading(true)
    try {
      await auth.mfaDisable(disableCode)
      setMfaEnabled(false)
      setDisableCode('')
      setSuccess('MFA disabled')
    } catch (err) {
      setError(err.message || 'Invalid code')
    } finally {
      setActionLoading(false)
    }
  }

  const copySecret = () => {
    if (setupData?.secret) {
      navigator.clipboard.writeText(setupData.secret)
      setSecretCopied(true)
      setTimeout(() => setSecretCopied(false), 2000)
    }
  }

  if (loading) return null

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <ShieldCheck size={20} className="text-print-400" />
        <h3 className="text-lg font-semibold">Two-Factor Authentication</h3>
        {mfaEnabled && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-green-900/40 text-green-400 border border-green-700">
            Enabled
          </span>
        )}
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {success && (
        <div className="mb-4 p-3 bg-green-900/30 border border-green-700 rounded-lg flex items-center gap-2 text-sm text-green-400">
          <CheckCircle size={16} /> {success}
        </div>
      )}

      {mfaEnabled && !setupData ? (
        /* MFA is enabled — show disable form */
        <div>
          <p className="text-sm text-farm-400 mb-4">
            Your account is protected with two-factor authentication.
          </p>
          <form onSubmit={handleDisable} className="flex items-end gap-3">
            <div className="flex-1">
              <label className="block text-sm text-farm-400 mb-1">Enter code to disable MFA</label>
              <input
                type="text"
                inputMode="numeric"
                maxLength={6}
                value={disableCode}
                onChange={(e) => setDisableCode(e.target.value.replace(/\D/g, ''))}
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-sm font-mono tracking-widest"
                placeholder="000000"
              />
            </div>
            <button
              type="submit"
              disabled={actionLoading || disableCode.length !== 6}
              className="flex items-center gap-2 px-4 py-2 rounded bg-red-600 hover:bg-red-700 text-white text-sm disabled:opacity-50"
            >
              <ShieldOff size={16} />
              {actionLoading ? 'Disabling...' : 'Disable MFA'}
            </button>
          </form>
        </div>
      ) : setupData ? (
        /* Setup in progress — show QR code and confirmation */
        <div>
          <p className="text-sm text-farm-400 mb-4">
            Scan the QR code with your authenticator app (Google Authenticator, Authy, etc.), then enter the 6-digit code to confirm.
          </p>
          <div className="flex flex-col items-center gap-4 mb-6">
            <img src={setupData.qr_code} alt="MFA QR Code" className="w-48 h-48 rounded-lg bg-white p-2" />
            <div className="flex items-center gap-2">
              <code className="text-xs bg-farm-800 px-3 py-1.5 rounded font-mono text-farm-300 select-all">
                {setupData.secret}
              </code>
              <button onClick={copySecret} className="p-1.5 rounded hover:bg-farm-700 text-farm-400">
                {secretCopied ? <CheckCircle size={14} className="text-green-400" /> : <Copy size={14} />}
              </button>
            </div>
          </div>
          <form onSubmit={handleConfirm} className="flex items-end gap-3">
            <div className="flex-1">
              <label className="block text-sm text-farm-400 mb-1">Verification code</label>
              <input
                type="text"
                inputMode="numeric"
                maxLength={6}
                value={confirmCode}
                onChange={(e) => setConfirmCode(e.target.value.replace(/\D/g, ''))}
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-sm font-mono tracking-widest"
                placeholder="000000"
                autoFocus
              />
            </div>
            <button
              type="submit"
              disabled={actionLoading || confirmCode.length !== 6}
              className="flex items-center gap-2 px-4 py-2 rounded bg-print-600 hover:bg-print-700 text-white text-sm disabled:opacity-50"
            >
              {actionLoading ? <Loader2 size={16} className="animate-spin" /> : <CheckCircle size={16} />}
              Confirm
            </button>
            <button
              type="button"
              onClick={() => { setSetupData(null); setConfirmCode(''); setError('') }}
              className="px-4 py-2 rounded bg-farm-800 hover:bg-farm-700 text-farm-300 text-sm"
            >
              Cancel
            </button>
          </form>
        </div>
      ) : (
        /* MFA not enabled — show enable button */
        <div>
          <p className="text-sm text-farm-400 mb-4">
            Add an extra layer of security to your account. You will need an authenticator app on your phone.
          </p>
          <button
            onClick={handleSetup}
            disabled={actionLoading}
            className="flex items-center gap-2 px-4 py-2 rounded bg-print-600 hover:bg-print-700 text-white text-sm disabled:opacity-50"
          >
            {actionLoading ? <Loader2 size={16} className="animate-spin" /> : <ShieldCheck size={16} />}
            Set Up Two-Factor Authentication
          </button>
        </div>
      )}
    </div>
  )
}
