import { useState, useRef, type ChangeEvent } from 'react'
import { Upload, AlertTriangle, CheckCircle } from 'lucide-react'

interface BackupRestoreProps {
  onRestored?: () => void
}

export default function BackupRestore({ onRestored }: BackupRestoreProps) {
  const [restoring, setRestoring] = useState(false)
  const [result, setResult] = useState<{ pre_restore_backup?: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmFile, setConfirmFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const handleFileSelect = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setConfirmFile(file)
    setResult(null)
    setError(null)
  }

  const handleRestore = async () => {
    if (!confirmFile) return
    setRestoring(true)
    setError(null)
    setResult(null)
    let rejected = false
    try {
      const formData = new FormData()
      formData.append('file', confirmFile)
      
      const res = await fetch('/api/backups/restore', {
        method: 'POST',
        credentials: 'include',
        body: formData,
      })
      if (!res.ok) {
        rejected = true
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Restore failed')
      }
      const data = await res.json()
      if (data.status !== 'staged' || data.restart_required !== true) {
        throw new Error('The server did not confirm a staged restore. Check server diagnostics before retrying.')
      }
      setResult(data)
      setConfirmFile(null)
      if (fileRef.current) fileRef.current.value = ''
      if (onRestored) onRestored()
    } catch (err) {
      setError(rejected
        ? (err instanceof Error ? err.message : 'Restore rejected')
        : 'The restore outcome could not be confirmed. It may already be staged. Check server diagnostics and the backup list before retrying or restarting.')
    } finally {
      setRestoring(false)
    }
  }

  const handleCancel = () => {
    setConfirmFile(null)
    if (fileRef.current) fileRef.current.value = ''
  }

  return (
    <div className="mt-4 pt-4 border-t border-[var(--brand-card-border)]">
      <h3 className="text-sm font-medium text-[var(--brand-text-secondary)] mb-2">Restore from Backup</h3>
      <p className="text-xs text-[var(--brand-text-muted)] mb-3">
        Upload a matching .db (SQLite) or .dump (PostgreSQL) database backup. Restore is staged for the next restart. Uploaded files and installation encryption keys require a separate data-volume backup.
      </p>

      {!confirmFile ? (
        <label className="inline-flex items-center gap-2 px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] rounded-md text-sm cursor-pointer transition-colors">
          <Upload size={16} />
          Choose backup file
          <input
            ref={fileRef}
            type="file"
            accept=".db,.dump"
            onChange={handleFileSelect}
            className="hidden"
          />
        </label>
      ) : (
        <div className="flex items-center gap-3 p-3 bg-amber-900/20 border border-amber-600/30 rounded-md">
          <AlertTriangle size={18} className="text-amber-400 shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-sm text-amber-200">
              Restore from <span className="font-mono">{confirmFile.name}</span>?
            </p>
            <p className="text-xs text-amber-400 mt-0.5">The next restart will replace the database. Plan a maintenance window and preserve the full data volume first.</p>
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              onClick={handleCancel}
              className="px-3 py-1.5 text-xs bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] rounded-md"
            >
              Cancel
            </button>
            <button
              onClick={handleRestore}
              disabled={restoring}
              className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-500 disabled:opacity-50 rounded-md font-medium"
            >
              {restoring ? 'Validating...' : 'Stage Restore'}
            </button>
          </div>
        </div>
      )}

      {result && (
        <div role="status" className="mt-3 p-3 bg-green-500/10 border border-green-500/30 rounded-md flex items-center gap-2">
          <CheckCircle size={16} className="text-green-400 shrink-0" />
          <span className="text-green-200 text-sm">
            Restore validated and staged. Restart ODIN during your maintenance window to apply it. Pre-restore database backup: {result.pre_restore_backup || 'Check the server backup list'}
          </span>
        </div>
      )}

      {error && (
        <div role="alert" className="mt-3 p-3 bg-red-500/10 border border-red-500/30 rounded-md flex items-center gap-2">
          <AlertTriangle size={16} className="text-red-400 shrink-0" />
          <span className="text-red-200 text-sm">{error}</span>
        </div>
      )}
    </div>
  )
}
