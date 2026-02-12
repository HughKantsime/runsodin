import { useState, useEffect } from 'react'
import { X, AlertTriangle } from 'lucide-react'

const FAILURE_REASONS = [
  { value: 'spaghetti', label: 'Spaghetti / Detached' },
  { value: 'adhesion', label: 'Bed Adhesion Failure' },
  { value: 'clog', label: 'Nozzle Clog' },
  { value: 'layer_shift', label: 'Layer Shift' },
  { value: 'stringing', label: 'Excessive Stringing' },
  { value: 'warping', label: 'Warping / Curling' },
  { value: 'filament_runout', label: 'Filament Runout' },
  { value: 'filament_tangle', label: 'Filament Tangle' },
  { value: 'power_loss', label: 'Power Loss' },
  { value: 'firmware_error', label: 'Firmware / HMS Error' },
  { value: 'user_cancelled', label: 'User Cancelled' },
  { value: 'other', label: 'Other' },
]

export default function FailureReasonModal({ jobId, jobName, onSubmit, onClose, existingReason, existingNotes }) {
  const [reason, setReason] = useState(existingReason || '')
  const [notes, setNotes] = useState(existingNotes || '')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    const handleEsc = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleEsc)
    return () => document.removeEventListener('keydown', handleEsc)
  }, [onClose])

  const handleSubmit = async () => {
    if (!reason) return
    setSubmitting(true)
    try {
      await onSubmit(jobId, reason, notes)
      onClose()
    } catch (err) {
      console.error('Failed to save failure reason:', err)
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center" role="dialog" aria-modal="true" aria-labelledby="failure-modal-title" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative bg-farm-950 border border-farm-700 rounded-lg shadow-2xl w-full max-w-md mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-farm-800">
          <div className="flex items-center gap-2">
            <AlertTriangle size={18} className="text-red-400" aria-hidden="true" />
            <h2 id="failure-modal-title" className="text-lg font-semibold text-farm-100">Print Failure</h2>
          </div>
          <button onClick={onClose} className="p-1 text-farm-400 hover:text-farm-200 rounded-lg" aria-label="Close failure reason dialog">
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          {jobName && (
            <p className="text-sm text-farm-400">Job: <span className="text-farm-200">{jobName}</span></p>
          )}

          <div>
            <label htmlFor="failure-reason" className="block text-sm font-medium text-farm-300 mb-1.5">
              Failure Reason <span className="text-red-400">*</span>
            </label>
            <select
              id="failure-reason"
              value={reason}
              onChange={e => setReason(e.target.value)}
              className="w-full bg-farm-900 border border-farm-700 rounded-md px-3 py-2 text-sm text-farm-100 focus:outline-none focus:border-amber-500"
            >
              <option value="">Select a reason...</option>
              {FAILURE_REASONS.map(r => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="failure-notes" className="block text-sm font-medium text-farm-300 mb-1.5">
              Notes <span className="text-farm-500">(optional)</span>
            </label>
            <textarea
              id="failure-notes"
              value={notes}
              onChange={e => setNotes(e.target.value)}
              placeholder="What happened? Any details that might help prevent this next time..."
              rows={3}
              className="w-full bg-farm-900 border border-farm-700 rounded-md px-3 py-2 text-sm text-farm-100 placeholder-farm-600 focus:outline-none focus:border-amber-500 resize-none"
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-3 px-5 py-4 border-t border-farm-800">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-farm-400 hover:text-farm-200 rounded-md"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!reason || submitting}
            className="px-4 py-2 text-sm bg-red-600 hover:bg-red-500 disabled:bg-farm-700 disabled:text-farm-500 text-white rounded-md transition-colors"
          >
            {submitting ? 'Saving...' : existingReason ? 'Update' : 'Mark as Failed'}
          </button>
        </div>
      </div>
    </div>
  )
}
