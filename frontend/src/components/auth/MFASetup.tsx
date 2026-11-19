import { useState, useEffect, useRef, useCallback } from 'react'
import { ShieldCheck, Loader2, Copy, CheckCircle, AlertTriangle } from 'lucide-react'
import RecoveryCodes from './RecoveryCodes'
import { auth } from '../../api'

export default function MFASetup({ principalKey }: { principalKey?: string | number } = {}) {
  const [mfaEnabled, setMfaEnabled] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(true)
  const [setupData, setSetupData] = useState(null)
  const [confirmCode, setConfirmCode] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const [remaining, setRemaining] = useState<number | null>(null)
  const [replacing, setReplacing] = useState(false)
  const [accountCode, setAccountCode] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [secretCopied, setSecretCopied] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)

  const mounted = useRef(false)
  const statusGeneration = useRef(0)
  const statusInFlight = useRef(false)
  const loadStatus = useCallback(async () => {
    if (!mounted.current || statusInFlight.current) return
    statusInFlight.current = true
    const generation = ++statusGeneration.current
    setLoading(true)
    setMfaEnabled(null)
    setError('')
    try {
      const data = await auth.mfaStatus()
      if (typeof data?.mfa_enabled !== 'boolean') throw new Error('Invalid MFA status')
      if (!mounted.current || generation !== statusGeneration.current) return
      setMfaEnabled(data.mfa_enabled)
      if (data.mfa_enabled) {
        try {
          const status = await auth.recoveryStatus()
          if (mounted.current && generation === statusGeneration.current) setRemaining(status.remaining)
        } catch (err) {
          if (mounted.current && generation === statusGeneration.current) {
            setError(err instanceof Error ? err.message : 'Unable to load the number of recovery codes.')
          }
        }
      }
    } catch (err) {
      if (mounted.current && generation === statusGeneration.current) {
        const serverRefusal = err instanceof Error && 'status' in err && [400, 401, 429].includes(Number(err.status))
        setError(serverRefusal ? err.message : 'Unable to load MFA status. Retry before making changes.')
      }
    } finally {
      if (generation === statusGeneration.current) {
        statusInFlight.current = false
        if (mounted.current) setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    setSetupData(null)
    setCodes(null)
    setRemaining(null)
    setReplacing(false)
    setConfirmCode('')
    setAccountCode('')
    setSuccess('')
    loadStatus()
    return () => {
      mounted.current = false
      ++statusGeneration.current
      statusInFlight.current = false
    }
  }, [principalKey, loadStatus])

  const handleSetup = async () => {
    setError('')
    setSuccess('')
    setActionLoading(true)
    try {
      const data = await auth.mfaSetup()
      setSetupData(data)
    } catch (err: any) {
      setError(err.message || 'Failed to start MFA setup')
    } finally {
      setActionLoading(false)
    }
  }

  const handleConfirm = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setActionLoading(true)
    try {
      const issued = await auth.mfaConfirm(confirmCode)
      setCodes(issued.recovery_codes)
      setRemaining(issued.recovery_codes.length)
      setMfaEnabled(true)
      setSetupData(null)
      setConfirmCode('')
      setSuccess('MFA enabled successfully')
    } catch (err: any) {
      setError(err.message || 'Invalid code')
    } finally {
      setActionLoading(false)
    }
  }

  const handleAccountCode = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setSuccess('')
    setActionLoading(true)
    try {
      if (replacing) {
        const issued = await auth.regenerateCodes(accountCode.trim())
        setCodes(issued.recovery_codes)
        setRemaining(issued.recovery_codes.length)
        setReplacing(false)
      } else {
        await auth.mfaDisable(accountCode.trim())
        setMfaEnabled(false)
        setRemaining(null)
        setSuccess('MFA disabled')
      }
    } catch (err: any) {
      setError(err.message || 'Invalid code')
    } finally {
      setAccountCode('')
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

  if (loading) return <p role="status">Loading MFA status...</p>
  if (mfaEnabled === null) return (
    <div>
      <h3 className="text-lg font-semibold mb-4">Two-Factor Authentication</h3>
      <p role="alert" className="text-sm text-red-400 mb-4">{error}</p>
      <button onClick={loadStatus} className="px-4 py-2 rounded bg-[var(--brand-input-bg)]">
        Retry MFA status
      </button>
    </div>
  )

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <ShieldCheck size={20} className="text-[var(--brand-primary)]" />
        <h3 className="text-lg font-semibold">Two-Factor Authentication</h3>
        {mfaEnabled && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-green-900/40 text-green-400 border border-green-700">
            Enabled
          </span>
        )}
      </div>

      {error && (
        <div role="alert" className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-md flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {success && (
        <div className="mb-4 p-3 bg-green-900/30 border border-green-700 rounded-md flex items-center gap-2 text-sm text-green-400">
          <CheckCircle size={16} /> {success}
        </div>
      )}

      {codes ? <RecoveryCodes codes={codes} onSaved={() => setCodes(null)} /> : mfaEnabled && !setupData ? (
        <div className="space-y-3">
          <p className="text-sm text-[var(--brand-text-secondary)]">Your account is protected with two-factor authentication.</p>
          {remaining !== null ? <>
            <p>{remaining} recovery {remaining === 1 ? 'code' : 'codes'} remaining.</p>
            {remaining <= 3 && <p role="status">{remaining === 0 ? 'You have no recovery codes left.' : 'Your recovery codes are running low.'} Generate a new set while you still have access.</p>}
          </> : <button type="button" className="underline" onClick={loadStatus}>Retry recovery code count</button>}
          {!replacing && <button type="button" className="underline min-h-11" disabled={actionLoading}
            onClick={() => { setReplacing(true); setAccountCode(''); setError(''); setSuccess('') }}>Regenerate recovery codes</button>}
          {replacing && <p>Generating a new set will invalidate all your old recovery codes.</p>}
          <form onSubmit={handleAccountCode} className="space-y-2">
            <label htmlFor="account-mfa-code" className="block text-sm">
              Enter an authenticator or recovery code to {replacing ? 'generate a new set' : 'disable MFA'}
            </label>
            <input id="account-mfa-code" type="text" autoComplete="off" spellCheck={false}
              value={accountCode} onChange={e => setAccountCode(e.target.value)}
              className="w-full rounded border border-[var(--brand-card-border)] bg-[var(--brand-input-bg)] px-3 py-2 font-mono" />
            <div className="flex flex-wrap gap-3">
              <button type="submit" disabled={actionLoading || !accountCode.trim()}
                className="rounded px-4 py-2 bg-[var(--brand-primary)] text-[var(--brand-on-primary)] disabled:opacity-50">
                {replacing ? 'Generate new codes' : 'Disable MFA'}
              </button>
              {replacing && <button type="button" disabled={actionLoading} className="underline"
                onClick={() => { setReplacing(false); setAccountCode(''); setError('') }}>Cancel</button>}
            </div>
          </form>
        </div>
      ) : setupData ? (
        /* Setup in progress — show QR code and confirmation */
        <div>
          <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
            Scan the QR code with your authenticator app (Google Authenticator, Authy, etc.), then enter the 6-digit code to confirm.
          </p>
          <div className="flex flex-col items-center gap-4 mb-6">
            <img src={setupData.qr_code} alt="MFA QR Code" className="w-48 h-48 rounded-md bg-white p-2" />
            <div className="flex items-center gap-2">
              <code className="text-xs bg-[var(--brand-input-bg)] px-3 py-1.5 rounded font-mono text-[var(--brand-text-secondary)] select-all">
                {setupData.secret}
              </code>
              <button aria-label={secretCopied ? "Secret copied" : "Copy secret"} onClick={copySecret} className="p-1.5 rounded hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)]">
                {secretCopied ? <CheckCircle size={14} className="text-green-400" /> : <Copy size={14} />}
              </button>
            </div>
          </div>
          <form onSubmit={handleConfirm} className="flex flex-wrap items-end gap-3">
            <div className="flex-1">
              <label htmlFor="mfa-setup-code" className="block text-sm text-[var(--brand-text-secondary)] mb-1">Verification code</label>
              <input
                type="text"
                id="mfa-setup-code"
                inputMode="numeric"
                maxLength={6}
                value={confirmCode}
                onChange={(e) => setConfirmCode(e.target.value.replace(/\D/g, ''))}
                className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded px-3 py-2 text-sm font-mono tracking-widest"
                placeholder="000000"
                autoFocus
              />
            </div>
            <button
              type="submit"
              disabled={actionLoading || confirmCode.length !== 6}
              className="flex items-center gap-2 px-4 py-2 rounded bg-[var(--brand-primary)] hover:opacity-90 text-white text-sm disabled:opacity-50"
            >
              {actionLoading ? <Loader2 size={16} className="animate-spin" /> : <CheckCircle size={16} />}
              Confirm
            </button>
            <button
              type="button"
              onClick={() => { setSetupData(null); setConfirmCode(''); setError('') }}
              className="px-4 py-2 rounded bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] text-sm"
            >
              Cancel
            </button>
          </form>
        </div>
      ) : (
        /* MFA not enabled — show enable button */
        <div>
          <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
            Add an extra layer of security to your account. You will need an authenticator app on your phone.
          </p>
          <button
            onClick={handleSetup}
            disabled={actionLoading}
            className="flex items-center gap-2 px-4 py-2 rounded bg-[var(--brand-primary)] hover:opacity-90 text-white text-sm disabled:opacity-50"
          >
            {actionLoading ? <Loader2 size={16} className="animate-spin" /> : <ShieldCheck size={16} />}
            Set Up Two-Factor Authentication
          </button>
        </div>
      )}
    </div>
  )
}
