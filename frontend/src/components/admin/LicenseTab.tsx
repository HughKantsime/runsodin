import { useState, useEffect, useRef, type ChangeEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { User, PublicLicense } from '../../types/user'
import { FileText, Key, Download, Sparkles } from 'lucide-react'
import { license as licenseApi, downloadBlob } from '../../api'
import { Button, Card, Input } from '../ui'

export default function LicenseTab() {
  const queryClient = useQueryClient()
  const { data: principal } = useQuery<User | null>({ queryKey: ['auth', 'me'], enabled: false })
  const audience = (user: User | null | undefined) => user?.role === 'admin' && Number.isInteger(user.id) && user.id > 0 ? JSON.stringify([user.id, user.role, user.group_id ?? null]) : null
  const currentAudience = audience(principal)
  const generation = useRef(0)
  const [installation, setInstallation] = useState<{ audience: string; id: string; device: string | null } | null>(null)
  const [idBusy, setIdBusy] = useState(false)
  const [idMessage, setIdMessage] = useState<string | null>(null)
  useEffect(() => {
    generation.current += 1
    setInstallation(null)
    setIdBusy(false)
    setIdMessage(null)
    return () => { generation.current += 1 }
  }, [currentAudience])
  const stillCurrent = (owner: string, stamp: number) => generation.current === stamp && audience(queryClient.getQueryData<User | null>(['auth', 'me'])) === owner
  const showInstallId = async () => {
    const owner = audience(queryClient.getQueryData<User | null>(['auth', 'me']))
    if (!owner || owner !== currentAudience || idBusy) return
    const stamp = generation.current
    setIdBusy(true)
    setIdMessage(null)
    try {
      const data = await licenseApi.getInstallationId()
      if (stillCurrent(owner, stamp)) {
        if (typeof data.installation_id !== 'string' || !data.installation_id) throw new Error('Invalid identity response')
        setInstallation({ audience: owner, id: data.installation_id, device: typeof data.device_fingerprint === 'string' ? data.device_fingerprint : null })
      }
    } catch {
      if (stillCurrent(owner, stamp)) setIdMessage('Could not load installation ID. Try again.')
    } finally {
      if (stillCurrent(owner, stamp)) setIdBusy(false)
    }
  }
  const [licenseInfo, setLicenseInfo] = useState<PublicLicense | null>(null)
  const [uploading, setUploading] = useState(false)
  const [activating, setActivating] = useState(false)
  const [licenseKey, setLicenseKey] = useState('')
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    loadLicense()
  }, [])

  const loadLicense = async () => {
    try {
      const data = await licenseApi.get()
      setLicenseInfo(data)
    } catch (e) { console.error('Failed to fetch license:', e) }
  }

  const handleActivate = async () => {
    if (!licenseKey.trim()) return
    setActivating(true)
    setMessage(null)
    try {
      const data = await licenseApi.activate(licenseKey.trim())
      setMessage({ type: 'success', text: `License activated: ${data.tier} tier` })
      setLicenseKey('')
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Activation failed: ' + (err instanceof Error ? err.message : 'Unknown error') })
    } finally {
      setActivating(false)
    }
  }

  const handleUpload = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setMessage(null)
    try {
      const data = await licenseApi.upload(file)
      setMessage({ type: 'success', text: `License activated: ${data.tier} tier` })
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Upload failed: ' + (err instanceof Error ? err.message : 'Unknown error') })
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleRemove = async () => {
    if (!confirm('Remove license and revert to Community tier?')) return
    try {
      await licenseApi.remove()
      setMessage({ type: 'success', text: 'License removed. Reverted to Community tier.' })
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to remove license' })
    }
  }

  const [trialEmail, setTrialEmail] = useState('')
  const [trialBusy, setTrialBusy] = useState<'online' | 'offline' | null>(null)
  const [trialResult, setTrialResult] = useState<{ sentTo?: string; code?: string; minutes?: number; error?: string; installationId?: string; device?: string } | null>(null)
  const requestTrial = async (offline: boolean) => {
    const email = trialEmail.trim()
    if (!email || trialBusy) return
    setTrialBusy(offline ? 'offline' : 'online')
    setTrialResult(null)
    try {
      const data = await licenseApi.requestTrial(email, offline)
      const identity = { installationId: data.installation_id, device: data.device_fingerprint }
      setTrialResult(offline ? { code: data.request_code, minutes: data.expires_in_minutes, ...identity } : { sentTo: email, ...identity })
    } catch (err) {
      setTrialResult({ error: err instanceof Error ? err.message : 'Trial request failed' })
    } finally {
      setTrialBusy(null)
    }
  }

  const [challengeCode, setChallengeCode] = useState('')
  const [exporting, setExporting] = useState<'activation' | 'unactivation' | null>(null)

  const saveJson = (data: unknown, filename: string) => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const handleExportActivationRequest = async () => {
    if (!licenseKey.trim() || !challengeCode.trim()) return
    setExporting('activation')
    setMessage(null)
    try {
      const data = await licenseApi.getActivationRequest(licenseKey.trim(), challengeCode.trim())
      saveJson(data, 'odin-activation-request.json')
      setMessage({ type: 'success', text: 'Activation request downloaded. Upload it on the license portal offline activation page.' })
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to export activation request: ' + (err instanceof Error ? err.message : 'Unknown error') })
    } finally {
      setExporting(null)
    }
  }

  const handleExportUnactivationRequest = async () => {
    if (!challengeCode.trim()) return
    if (!confirm('Generating the unactivation request removes the license from this server now (it reverts to Community). Continue?')) return
    setExporting('unactivation')
    setMessage(null)
    try {
      const data = await licenseApi.getUnactivationRequest(challengeCode.trim())
      saveJson(data, 'odin-unactivation-request.json')
      setMessage({ type: 'success', text: 'Unactivation request downloaded and the license was removed from this server. Upload the file on the license portal to free the grant.' })
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to export unactivation request: ' + (err instanceof Error ? err.message : 'Unknown error') })
    } finally {
      setExporting(null)
    }
  }

  const copyInstallId = async () => {
    const owner = audience(queryClient.getQueryData<User | null>(['auth', 'me']))
    if (!owner || owner !== currentAudience || installation?.audience !== owner || idBusy) return
    const stamp = generation.current
    setIdBusy(true)
    setIdMessage(null)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(installation.id)
      if (stillCurrent(owner, stamp)) setIdMessage('Installation ID copied to clipboard')
    } catch {
      if (stillCurrent(owner, stamp)) setIdMessage('Could not copy. Select the installation ID and copy it manually.')
    } finally {
      if (stillCurrent(owner, stamp)) setIdBusy(false)
    }
  }

  const tier = licenseInfo?.tier || 'community'
  const trialActive = licenseInfo?.trial === true && licenseInfo.valid === true
  const trialEnded = licenseInfo?.trial === true && licenseInfo.expired === true
  const trialDaysLeft = licenseInfo?.trial_days_left ?? 0
  const managedExternally = licenseInfo?.managed_externally === true
  const tierColors: Record<string, string> = {
    community: 'text-[var(--brand-text-secondary)]',
    pro: 'text-[var(--brand-primary)]',
    education: 'text-blue-400',
    enterprise: 'text-purple-400',
  }

  return (
    <div className="space-y-6">
      <Card padding="lg">
        <div className="flex items-center gap-2 mb-4">
          <FileText size={18} className="text-[var(--brand-primary)]" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Current License</h3>
        </div>
        {(trialActive || trialEnded) && (
          <div
            role="status"
            data-testid="license-trial-status"
            className={`mb-4 flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm ${trialActive ? 'border-[var(--brand-primary)]/40 bg-[var(--brand-primary)]/10' : 'border-amber-500/40 bg-amber-500/10'}`}
          >
            <div>
              <p className="font-semibold text-[var(--brand-text-primary)]">
                {trialActive ? `Pro trial — ${trialDaysLeft} ${trialDaysLeft === 1 ? 'day' : 'days'} left` : 'Trial ended'}
              </p>
              <p className="text-xs text-[var(--brand-text-secondary)]">
                {trialActive
                  ? 'All Pro features are on until the trial ends. Your data is kept when it ends.'
                  : 'Pro features no longer accept new or changed records. Your data is kept: you can still view, export and delete it.'}
              </p>
            </div>
            <a
              href="https://runsodin.com/pricing"
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs font-medium text-[var(--brand-primary)] underline"
            >
              Buy a license
            </a>
          </div>
        )}
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-[var(--brand-text-muted)]">Tier:</span>
            <span className={`ml-2 font-semibold capitalize ${tierColors[tier] || 'text-[var(--brand-text-secondary)]'}`}>{tier}</span>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)]">License:</span>
            <span className="ml-2 text-[var(--brand-text-primary)]">BSL 1.1 (converts to Apache 2.0 on 2029-02-07)</span>
          </div>
          {licenseInfo?.expires_at && (
            <div>
              <span className="text-[var(--brand-text-muted)]">Expires:</span>
              <span className="ml-2 text-[var(--brand-text-primary)]">{licenseInfo.expires_at}</span>
            </div>
          )}
          <div>
            <span className="text-[var(--brand-text-muted)]">Max printers:</span>
            <span className="ml-2 text-[var(--brand-text-primary)]">{licenseInfo?.max_printers === null ? 'Unlimited' : (licenseInfo?.max_printers ?? 5)}</span>
          </div>
          <div>
            <span className="text-[var(--brand-text-muted)]">Max users:</span>
            <span className="ml-2 text-[var(--brand-text-primary)]">{licenseInfo?.max_users === null ? 'Unlimited' : (licenseInfo?.max_users ?? 1)}</span>
          </div>
        </div>

        {currentAudience && (
          <div className="mt-4 pt-4 border-t border-[var(--brand-card-border)]">
            <span className="text-[var(--brand-text-muted)] text-sm">Installation ID:</span>
            <div className="flex items-center gap-2 mt-1">
              <code className="text-xs text-[var(--brand-text-secondary)] bg-[var(--brand-card-bg)] px-2 py-1 rounded font-mono flex-1 select-all">{installation?.audience === currentAudience ? installation.id : ''}</code>
              <button disabled={idBusy} onClick={installation?.audience === currentAudience ? copyInstallId : showInstallId} className="text-xs text-[var(--brand-primary)] hover:text-[var(--brand-primary)] transition-colors px-2 py-1">{installation?.audience === currentAudience ? 'Copy' : 'Show installation ID'}</button>
            </div>
            {installation?.audience === currentAudience && installation.device && (
              <p className="mt-2 text-xs text-[var(--brand-text-muted)]" data-testid="device-key">
                Device key: <code className="font-mono text-[var(--brand-text-secondary)]">{installation.device}</code>
                {' '}&mdash; a Pro trial confirmation email and page show this installation ID and device key. Only confirm if they match.
              </p>
            )}
          </div>
        )}
      </Card>

      {idMessage && <p role="status">{idMessage}</p>}

      {!managedExternally && licenseInfo && licenseInfo.valid !== true && licenseInfo.trial !== true && (
        <Card padding="lg">
          <div className="flex items-center gap-2 mb-3">
            <Sparkles size={18} className="text-[var(--brand-primary)]" />
            <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Start 14-day Pro trial</h3>
          </div>
          <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
            ODIN signs the request with this installation&apos;s device key. We email a confirmation link; after you confirm,
            the license portal shows your trial key once. Paste it under Activate License below. One trial per installation
            and per email address; your data is kept when it ends.
          </p>
          <div className="flex flex-wrap gap-2">
            <Input aria-label="Trial email" type="email" value={trialEmail} onChange={e => setTrialEmail(e.target.value)}
              placeholder="you@example.com" className="flex-1 min-w-[14rem] bg-[var(--brand-card-bg)]" autoComplete="email" />
            <Button variant="primary" loading={trialBusy === 'online'} disabled={!trialEmail.trim() || trialBusy !== null} onClick={() => requestTrial(false)}>
              Send trial request
            </Button>
            <Button variant="secondary" loading={trialBusy === 'offline'} disabled={!trialEmail.trim() || trialBusy !== null} onClick={() => requestTrial(true)}>
              Get request code
            </Button>
          </div>
          {trialResult?.sentTo && (
            <p role="status" className="mt-3 text-sm text-green-400">
              Check {trialResult.sentTo} for a confirmation link. It works once and expires in 24 hours.
            </p>
          )}
          {trialResult?.code && (
            <div className="mt-3 space-y-2">
              <p className="text-xs text-[var(--brand-text-secondary)]">
                Offline: paste this code on{' '}
                <a href="https://runsodin.com/portal/trial" target="_blank" rel="noopener noreferrer" className="text-[var(--brand-primary)] underline">runsodin.com/portal/trial</a>{' '}
                within {trialResult.minutes ?? 15} minutes, from any computer with internet access.
              </p>
              <textarea aria-label="Trial request code" readOnly value={trialResult.code} rows={3}
                className="w-full font-mono text-xs rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-2 text-[var(--brand-text-primary)]"
                onFocus={e => e.currentTarget.select()} />
            </div>
          )}
          {(trialResult?.sentTo || trialResult?.code) && trialResult.installationId && (
            <div data-testid="trial-identity" className="mt-3 rounded-md border border-[var(--brand-card-border)] p-3 text-xs text-[var(--brand-text-secondary)]">
              <p className="mb-1">The confirmation email and page show this server&apos;s identity:</p>
              <p>Installation ID: <code className="font-mono text-[var(--brand-text-primary)]">{trialResult.installationId}</code></p>
              <p>Device key: <code className="font-mono text-[var(--brand-text-primary)]">{trialResult.device}</code></p>
              <p className="mt-1 font-medium text-[var(--brand-text-primary)]">Only confirm if this matches Settings &gt; License in your ODIN.</p>
            </div>
          )}
          {trialResult?.error && (
            <p role="alert" className="mt-3 text-sm text-red-400">Trial request failed: {trialResult.error}</p>
          )}
        </Card>
      )}

      {managedExternally ? (
        <Card padding="lg">
          <div className="flex items-center gap-2 mb-3">
            <Key size={18} className="text-blue-400" />
            <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Externally Managed License</h3>
          </div>
          <p className="text-sm text-[var(--brand-text-secondary)]">
            This deployment mounts its signed license read-only. License changes are managed by the sandbox administrator outside ODIN.
          </p>
        </Card>
      ) : <Card padding="lg">
        <div className="flex items-center gap-2 mb-4">
          <Key size={18} className="text-[var(--brand-primary)]" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Activate License</h3>
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          Enter your license key to activate online. The license will be bound to this installation.
        </p>
        <div className="flex gap-2">
          <Input aria-label="License key"
            type="text"
            value={licenseKey}
            onChange={e => setLicenseKey(e.target.value)}
            placeholder="ODIN-XXXX-XXXX-XXXX"
            className="flex-1 bg-[var(--brand-card-bg)]"
            onKeyDown={e => e.key === 'Enter' && handleActivate()}
          />
          <Button
            variant="primary"
            loading={activating}
            disabled={!licenseKey.trim()}
            onClick={handleActivate}
          >
            Activate
          </Button>
        </div>

        <div className="mt-4 rounded-md border border-[var(--brand-card-border)] p-3">
          <h4 className="text-sm font-medium text-[var(--brand-text-primary)] mb-1">Offline (air-gapped) activation</h4>
          <p className="text-xs text-[var(--brand-text-secondary)] mb-3">
            On a machine with internet access, open the license portal offline activation page (runsodin.com/portal/activate),
            enter this installation ID and get a challenge code. Enter the challenge code here with your license key above to
            download a signed activation request, or the challenge code alone to download a signed unactivation request.
          </p>
          <div className="flex flex-wrap gap-2 items-center">
            <Input
              type="text"
              aria-label="Challenge code"
              value={challengeCode}
              onChange={e => setChallengeCode(e.target.value)}
              placeholder="Challenge code from the license portal"
              className="flex-1 min-w-[12rem] bg-[var(--brand-card-bg)]"
              spellCheck={false}
              autoComplete="off"
            />
            <Button
              variant="secondary"
              icon={Download}
              loading={exporting === 'activation'}
              disabled={!licenseKey.trim() || !challengeCode.trim() || exporting !== null}
              onClick={handleExportActivationRequest}
            >
              Download activation request
            </Button>
            {licenseInfo?.valid === true && (
              <Button
                variant="secondary"
                icon={Download}
                loading={exporting === 'unactivation'}
                disabled={!challengeCode.trim() || exporting !== null}
                onClick={handleExportUnactivationRequest}
              >
                Download unactivation request
              </Button>
            )}
          </div>
        </div>

        <div className="relative my-5">
          <div className="absolute inset-0 flex items-center">
            <div className="w-full border-t border-[var(--brand-card-border)]" />
          </div>
          <div className="relative flex justify-center text-xs">
            <span className="px-3 text-[var(--brand-text-muted)]" style={{ backgroundColor: 'var(--brand-card-bg)' }}>or upload license file</span>
          </div>
        </div>

        <div className="flex gap-3">
          <label className="flex-1">
            <input
              ref={fileInputRef}
              type="file"
              accept=".license"
              onChange={handleUpload}
              className="hidden"
            />
            <div className={`w-full px-4 py-3 rounded-md border-2 border-dashed text-center cursor-pointer transition-colors text-sm ${uploading ? 'border-[var(--brand-card-border)] text-[var(--brand-text-muted)]' : 'border-[var(--brand-card-border)] text-[var(--brand-text-secondary)] hover:border-[var(--brand-primary)] hover:text-[var(--brand-primary)]'}`}>
              {uploading ? 'Uploading...' : 'Click to select .license file'}
            </div>
          </label>
        </div>
        {tier !== 'community' && (
          <button
            onClick={handleRemove}
            className="mt-3 text-xs text-red-400 hover:text-red-300 transition-colors"
          >
            Remove license (revert to Community)
          </button>
        )}
        {message && (
          <div className={`mt-3 text-sm px-3 py-2 rounded-md ${message.type === 'success' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
            {message.text}
          </div>
        )}
      </Card>}
    </div>
  )
}
