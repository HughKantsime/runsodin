#!/usr/bin/env python3
"""
Print failure logging UI for Jobs page.
Backend already has: FAILURE_REASONS, PATCH /api/jobs/{id}/failure, fail_reason/fail_notes columns.
This wires the frontend.
"""

BASE = "/opt/printfarm-scheduler"
FRONTEND = f"{BASE}/frontend/src"

# =============================================================================
# 1. Create FailureReasonModal component
# =============================================================================

modal_content = '''import { useState, useEffect } from 'react'
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
    <div className="fixed inset-0 z-[200] flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative bg-farm-950 border border-farm-700 rounded-lg shadow-2xl w-full max-w-md mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-farm-800">
          <div className="flex items-center gap-2">
            <AlertTriangle size={18} className="text-red-400" />
            <h2 className="text-lg font-semibold text-farm-100">Print Failure</h2>
          </div>
          <button onClick={onClose} className="p-1 text-farm-400 hover:text-farm-200 rounded">
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          {jobName && (
            <p className="text-sm text-farm-400">Job: <span className="text-farm-200">{jobName}</span></p>
          )}

          <div>
            <label className="block text-sm font-medium text-farm-300 mb-1.5">
              Failure Reason <span className="text-red-400">*</span>
            </label>
            <select
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
            <label className="block text-sm font-medium text-farm-300 mb-1.5">
              Notes <span className="text-farm-500">(optional)</span>
            </label>
            <textarea
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
'''

with open(f"{FRONTEND}/components/FailureReasonModal.jsx", "w") as f:
    f.write(modal_content)
print("✅ Created FailureReasonModal.jsx")

# =============================================================================
# 2. Wire into Jobs.jsx
# =============================================================================

jobs_path = f"{FRONTEND}/pages/Jobs.jsx"
with open(jobs_path, "r") as f:
    jobs = f.read()

if "FailureReasonModal" not in jobs:
    # Add imports
    jobs = jobs.replace(
        "import { canDo } from '../permissions'",
        "import { canDo } from '../permissions'\nimport FailureReasonModal from '../components/FailureReasonModal'\nimport { updateJobFailure } from '../api'"
    )

    # Add AlertTriangle to lucide import if not present
    if "AlertTriangle" not in jobs.split("from 'lucide-react'")[0]:
        jobs = jobs.replace(
            "} from 'lucide-react'",
            ", AlertTriangle } from 'lucide-react'",
            1
        )

    # Add state for failure modal — before drag-and-drop section
    jobs = jobs.replace(
        "  // Drag-and-drop queue reorder",
        "  // Failure reason modal\n"
        "  const [failureModal, setFailureModal] = React.useState(null)\n\n"
        "  // Drag-and-drop queue reorder"
    )

    # Update handleAction to handle new actions + add failure submit handler
    jobs = jobs.replace(
        """  const handleAction = (action, jobId) => {
    switch (action) {
      case 'start': startJob.mutate(jobId); break
      case 'complete': completeJob.mutate(jobId); break
      case 'cancel': cancelJob.mutate(jobId); break
      case 'repeat': repeatJob.mutate(jobId); break
      case 'delete': if (confirm('Delete this job?')) deleteJob.mutate(jobId); break
    }
  }""",
        """  const handleAction = (action, jobId, jobName, existingReason, existingNotes) => {
    switch (action) {
      case 'start': startJob.mutate(jobId); break
      case 'complete': completeJob.mutate(jobId); break
      case 'cancel': cancelJob.mutate(jobId); break
      case 'repeat': repeatJob.mutate(jobId); break
      case 'delete': if (confirm('Delete this job?')) deleteJob.mutate(jobId); break
      case 'markFailed':
        cancelJob.mutate(jobId, {
          onSuccess: () => setFailureModal({ jobId, jobName })
        })
        break
      case 'failReason':
        setFailureModal({ jobId, jobName, existingReason, existingNotes })
        break
    }
  }

  const handleFailureSubmit = async (jobId, reason, notes) => {
    await updateJobFailure(jobId, reason, notes)
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
    queryClient.invalidateQueries({ queryKey: ['print-jobs'] })
  }"""
    )

    # Add "Mark Failed" button next to Complete on printing jobs
    jobs = jobs.replace(
        """{job.status === 'printing' && (
            <button onClick={() => onAction('complete', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded" title="Complete">
              <CheckCircle size={16} />
            </button>
          )}""",
        """{job.status === 'printing' && (
            <>
              <button onClick={() => onAction('complete', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded" title="Complete">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('markFailed', job.id, job.item_name)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded" title="Mark Failed">
                <AlertTriangle size={16} />
              </button>
            </>
          )}"""
    )

    # Add "Add/Edit Reason" button on failed jobs
    jobs = jobs.replace(
        """{canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (""",
        """{job.status === 'failed' && (
            <button onClick={() => onAction('failReason', job.id, job.item_name, job.fail_reason, job.fail_notes)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded" title={job.fail_reason ? 'Edit Failure Reason' : 'Add Failure Reason'}>
              <AlertTriangle size={16} />
            </button>
          )}
          {canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && ("""
    )

    # Display fail_reason on job row
    jobs = jobs.replace(
        """{job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}""",
        """{job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}
        {job.fail_reason && (
          <div className="text-xs text-red-400 truncate max-w-xs">
            ⚠ {job.fail_reason.replace(/_/g, ' ')}{job.fail_notes ? `: ${job.fail_notes}` : ''}
          </div>
        )}"""
    )

    # Add modal render after the table
    jobs = jobs.replace(
        "      </table>",
        """      </table>
      {failureModal && (
        <FailureReasonModal
          jobId={failureModal.jobId}
          jobName={failureModal.jobName}
          existingReason={failureModal.existingReason}
          existingNotes={failureModal.existingNotes}
          onSubmit={handleFailureSubmit}
          onClose={() => setFailureModal(null)}
        />
      )}""",
        1
    )

    with open(jobs_path, "w") as f:
        f.write(jobs)
    print("✅ Wired failure logging into Jobs.jsx")
else:
    print("✓ FailureReasonModal already in Jobs.jsx")

print()
print("=" * 60)
print("✅ Print Failure Logging UI complete!")
print("=" * 60)
print("""
What's new:
  - FailureReasonModal.jsx — dropdown with 12 reasons + notes textarea
  - Mark Failed button (⚠) on printing jobs — cancels then opens modal
  - Add/Edit Reason button (⚠) on failed jobs — opens modal
  - Fail reason displayed inline on job rows (red text)
  
Flow:
  1. Job is printing → click ⚠ → job cancels → failure modal opens
  2. Select reason (required) + optional notes → Save
  3. Failed job shows "⚠ spaghetti: Detached at layer 42" inline
  4. Click ⚠ on any failed job to add/edit reason later

Deploy:
  python3 add_failure_logging.py
  cd frontend && npm run build
  systemctl restart printfarm-backend
""")
