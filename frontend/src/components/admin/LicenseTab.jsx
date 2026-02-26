import { useState, useEffect, useRef } from 'react'
import { FileText, Key, Download } from 'lucide-react'
import { license as licenseApi, downloadBlob } from '../../api'

export default function LicenseTab() {
  const [licenseInfo, setLicenseInfo] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [activating, setActivating] = useState(false)
  const [licenseKey, setLicenseKey] = useState('')
  const [message, setMessage] = useState(null)
  const fileInputRef = useRef(null)

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
      setMessage({ type: 'error', text: 'Activation failed: ' + err.message })
    } finally {
      setActivating(false)
    }
  }

  const handleUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setMessage(null)
    try {
      const data = await licenseApi.upload(file)
      setMessage({ type: 'success', text: `License activated: ${data.tier} tier` })
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Upload failed: ' + err.message })
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

  const handleExportActivationRequest = async () => {
    try {
      const data = await licenseApi.getActivationRequest()
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'odin-activation-request.json'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to export activation request' })
    }
  }

  const copyInstallId = () => {
    if (licenseInfo?.installation_id) {
      navigator.clipboard.writeText(licenseInfo.installation_id)
      setMessage({ type: 'success', text: 'Installation ID copied to clipboard' })
    }
  }

  const tier = licenseInfo?.tier || 'community'
  const tierColors = {
    community: 'text-farm-400',
    pro: 'text-amber-400',
    education: 'text-blue-400',
    enterprise: 'text-purple-400',
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg p-5" style={{ backgroundColor: 'var(--brand-card-bg)', borderColor: 'var(--brand-sidebar-border)', border: '1px solid' }}>
        <div className="flex items-center gap-2 mb-4">
          <FileText size={18} className="text-print-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Current License</h3>
        </div>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-farm-500">Tier:</span>
            <span className={`ml-2 font-semibold capitalize ${tierColors[tier] || 'text-farm-300'}`}>{tier}</span>
          </div>
          <div>
            <span className="text-farm-500">License:</span>
            <span className="ml-2 text-farm-200">BSL 1.1 (converts to Apache 2.0 on 2029-02-07)</span>
          </div>
          {licenseInfo?.licensee && (
            <div>
              <span className="text-farm-500">Licensed to:</span>
              <span className="ml-2 text-farm-200">{licenseInfo.licensee}</span>
            </div>
          )}
          {licenseInfo?.expires && (
            <div>
              <span className="text-farm-500">Expires:</span>
              <span className="ml-2 text-farm-200">{new Date(licenseInfo.expires).toLocaleDateString()}</span>
            </div>
          )}
          <div>
            <span className="text-farm-500">Max printers:</span>
            <span className="ml-2 text-farm-200">{licenseInfo?.max_printers === -1 ? 'Unlimited' : (licenseInfo?.max_printers || 5)}</span>
          </div>
          <div>
            <span className="text-farm-500">Max users:</span>
            <span className="ml-2 text-farm-200">{licenseInfo?.max_users === -1 ? 'Unlimited' : (licenseInfo?.max_users || 1)}</span>
          </div>
        </div>

        {licenseInfo?.installation_id && (
          <div className="mt-4 pt-4 border-t border-farm-800">
            <span className="text-farm-500 text-sm">Installation ID:</span>
            <div className="flex items-center gap-2 mt-1">
              <code className="text-xs text-farm-300 bg-farm-900 px-2 py-1 rounded font-mono flex-1 select-all">{licenseInfo.installation_id}</code>
              <button onClick={copyInstallId} className="text-xs text-print-400 hover:text-print-300 transition-colors px-2 py-1">Copy</button>
            </div>
          </div>
        )}
      </div>

      <div className="rounded-lg p-5" style={{ backgroundColor: 'var(--brand-card-bg)', borderColor: 'var(--brand-sidebar-border)', border: '1px solid' }}>
        <div className="flex items-center gap-2 mb-4">
          <Key size={18} className="text-print-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Activate License</h3>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Enter your license key to activate online. The license will be bound to this installation.
        </p>
        <div className="flex gap-2">
          <input
            type="text"
            value={licenseKey}
            onChange={e => setLicenseKey(e.target.value)}
            placeholder="ODIN-XXXX-XXXX-XXXX"
            className="flex-1 px-3 py-2 rounded-lg bg-farm-900 border border-farm-700 text-farm-200 text-sm placeholder-farm-600 focus:outline-none focus:border-print-500"
            onKeyDown={e => e.key === 'Enter' && handleActivate()}
          />
          <button
            onClick={handleActivate}
            disabled={activating || !licenseKey.trim()}
            className="px-4 py-2 rounded-lg bg-print-600 text-white text-sm font-medium hover:bg-print-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {activating ? 'Activating...' : 'Activate'}
          </button>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={handleExportActivationRequest}
            className="text-xs text-farm-400 hover:text-print-400 transition-colors flex items-center gap-1"
          >
            <Download size={14} />
            Export activation request (offline)
          </button>
        </div>

        <div className="relative my-5">
          <div className="absolute inset-0 flex items-center">
            <div className="w-full border-t border-farm-700" />
          </div>
          <div className="relative flex justify-center text-xs">
            <span className="px-3 text-farm-500" style={{ backgroundColor: 'var(--brand-card-bg)' }}>or upload license file</span>
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
            <div className={`w-full px-4 py-3 rounded-lg border-2 border-dashed text-center cursor-pointer transition-colors text-sm ${uploading ? 'border-farm-600 text-farm-500' : 'border-farm-700 text-farm-400 hover:border-print-500 hover:text-print-400'}`}>
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
          <div className={`mt-3 text-sm px-3 py-2 rounded-lg ${message.type === 'success' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
            {message.text}
          </div>
        )}
      </div>
    </div>
  )
}
