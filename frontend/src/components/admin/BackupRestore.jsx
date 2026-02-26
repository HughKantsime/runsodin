import { useState, useRef } from 'react'
import { Upload, AlertTriangle, CheckCircle } from 'lucide-react'

export default function BackupRestore({ onRestored }) {
  const [restoring, setRestoring] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [confirmFile, setConfirmFile] = useState(null)
  const fileRef = useRef(null)

  const handleFileSelect = (e) => {
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
    try {
      const formData = new FormData()
      formData.append('file', confirmFile)
      
      const res = await fetch('/api/backups/restore', {
        method: 'POST',
        credentials: 'include',
        body: formData,
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Restore failed')
      }
      const data = await res.json()
      setResult(data)
      setConfirmFile(null)
      if (fileRef.current) fileRef.current.value = ''
      if (onRestored) onRestored()
    } catch (err) {
      setError(err.message)
    } finally {
      setRestoring(false)
    }
  }

  const handleCancel = () => {
    setConfirmFile(null)
    if (fileRef.current) fileRef.current.value = ''
  }

  return (
    <div className="mt-4 pt-4 border-t border-farm-700">
      <h3 className="text-sm font-medium text-farm-300 mb-2">Restore from Backup</h3>
      <p className="text-xs text-farm-500 mb-3">
        Upload a .db backup file to restore. A safety backup will be created automatically before restoring.
      </p>

      {!confirmFile ? (
        <label className="inline-flex items-center gap-2 px-4 py-2 bg-farm-700 hover:bg-farm-600 rounded-lg text-sm cursor-pointer transition-colors">
          <Upload size={16} />
          Choose backup file
          <input
            ref={fileRef}
            type="file"
            accept=".db"
            onChange={handleFileSelect}
            className="hidden"
          />
        </label>
      ) : (
        <div className="flex items-center gap-3 p-3 bg-amber-900/20 border border-amber-600/30 rounded-lg">
          <AlertTriangle size={18} className="text-amber-400 flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-sm text-amber-200">
              Restore from <span className="font-mono">{confirmFile.name}</span>?
            </p>
            <p className="text-xs text-amber-400 mt-0.5">This will replace the current database.</p>
          </div>
          <div className="flex gap-2 flex-shrink-0">
            <button
              onClick={handleCancel}
              className="px-3 py-1.5 text-xs bg-farm-700 hover:bg-farm-600 rounded-lg"
            >
              Cancel
            </button>
            <button
              onClick={handleRestore}
              disabled={restoring}
              className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-500 disabled:opacity-50 rounded-lg font-medium"
            >
              {restoring ? 'Restoring...' : 'Confirm Restore'}
            </button>
          </div>
        </div>
      )}

      {result && (
        <div className="mt-3 p-3 bg-green-500/10 border border-green-500/30 rounded-lg flex items-center gap-2">
          <CheckCircle size={16} className="text-green-400 flex-shrink-0" />
          <span className="text-green-200 text-sm">
            Database restored successfully. Safety backup: {result.safety_backup}
          </span>
        </div>
      )}

      {error && (
        <div className="mt-3 p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2">
          <AlertTriangle size={16} className="text-red-400 flex-shrink-0" />
          <span className="text-red-200 text-sm">{error}</span>
        </div>
      )}
    </div>
  )
}
