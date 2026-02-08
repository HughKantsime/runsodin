#!/usr/bin/env python3
"""
Edit Jobs.jsx to add approval workflow UI.
Run on server: python3 edit_jobs_approval.py

Changes:
1. Add 'submitted' and 'rejected' to status options
2. Add 'Awaiting Approval' tab
3. Add approve/reject/resubmit buttons in JobRow
4. Add RejectModal component
5. Add approval mutations
6. Import approveJob, rejectJob, resubmitJob from api
"""

JOBS_JSX = "/opt/printfarm-scheduler/frontend/src/pages/Jobs.jsx"

with open(JOBS_JSX, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Add imports for approval API functions and icons
# ============================================================

old_import = "import { jobs, models, printers, scheduler } from '../api'"
new_import = "import { jobs, models, printers, scheduler, approveJob, rejectJob, resubmitJob, getApprovalSetting } from '../api'"

if old_import in content and "approveJob" not in content:
    content = content.replace(old_import, new_import)
    changes += 1
    print("✓ Added approval imports to api import line")
else:
    if "approveJob" in content:
        print("· approveJob already imported")
    else:
        print("✗ Could not find api import line")

# Add CheckSquare and MessageSquare icons
old_icons = "Plus, Play, CheckCircle, XCircle, RotateCcw, Trash2, Filter, Search, ArrowUp, ArrowDown, ArrowUpDown, ShoppingCart, Layers, Zap , RefreshCw"
new_icons = "Plus, Play, CheckCircle, XCircle, RotateCcw, Trash2, Filter, Search, ArrowUp, ArrowDown, ArrowUpDown, ShoppingCart, Layers, Zap, RefreshCw, Clock, MessageSquare"

if old_icons in content:
    content = content.replace(old_icons, new_icons)
    changes += 1
    print("✓ Added Clock, MessageSquare to icon imports")

# ============================================================
# 2. Add 'submitted' and 'rejected' to status options and statusOrder
# ============================================================

old_status_options = """const statusOptions = [
  { value: '', label: 'All Status' },
  { value: 'pending', label: 'Pending' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
]"""

new_status_options = """const statusOptions = [
  { value: '', label: 'All Status' },
  { value: 'submitted', label: 'Submitted' },
  { value: 'pending', label: 'Pending' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'rejected', label: 'Rejected' },
]"""

if old_status_options in content:
    content = content.replace(old_status_options, new_status_options)
    changes += 1
    print("✓ Added submitted/rejected to statusOptions")

old_status_order = "const statusOrder = { printing: 0, scheduled: 1, pending: 2, failed: 3, completed: 4 }"
new_status_order = "const statusOrder = { submitted: 0, printing: 1, scheduled: 2, pending: 3, rejected: 4, failed: 5, completed: 6 }"

if old_status_order in content:
    content = content.replace(old_status_order, new_status_order)
    changes += 1
    print("✓ Updated statusOrder to include submitted/rejected")

# ============================================================
# 3. Add 'Awaiting Approval' tab
# ============================================================

old_tabs = """const jobTypeTabs = [
  { value: 'all', label: 'All Jobs', icon: Layers },
  { value: 'order', label: 'Order Jobs', icon: ShoppingCart },
  { value: 'adhoc', label: 'Ad-hoc', icon: null },
]"""

new_tabs = """const jobTypeTabs = [
  { value: 'all', label: 'All Jobs', icon: Layers },
  { value: 'approval', label: 'Awaiting Approval', icon: Clock },
  { value: 'order', label: 'Order Jobs', icon: ShoppingCart },
  { value: 'adhoc', label: 'Ad-hoc', icon: null },
]"""

if old_tabs in content:
    content = content.replace(old_tabs, new_tabs)
    changes += 1
    print("✓ Added 'Awaiting Approval' tab")

# ============================================================
# 4. Add submitted/rejected status colors to JobRow
# ============================================================

old_status_colors = """  const statusColors = {
    pending: 'text-status-pending',
    scheduled: 'text-status-scheduled',
    printing: 'text-status-printing',
    completed: 'text-status-completed',
    failed: 'text-status-failed',
  }"""

new_status_colors = """  const statusColors = {
    submitted: 'text-amber-400',
    pending: 'text-status-pending',
    scheduled: 'text-status-scheduled',
    printing: 'text-status-printing',
    completed: 'text-status-completed',
    failed: 'text-status-failed',
    rejected: 'text-red-400',
  }"""

if old_status_colors in content:
    content = content.replace(old_status_colors, new_status_colors)
    changes += 1
    print("✓ Added submitted/rejected status colors")

# ============================================================
# 5. Add approve/reject/resubmit buttons in JobRow actions
# ============================================================

old_actions = """        <div className="flex items-center gap-1">
          {job.status === 'scheduled' && (
            <button onClick={() => onAction('start', job.id)} className="p-1.5 text-print-400 hover:bg-print-900/50 rounded" title="Start Print">
              <Play size={16} />
            </button>
          )}"""

new_actions = """        <div className="flex items-center gap-1">
          {job.status === 'submitted' && (
            <>
              <button onClick={() => onAction('approve', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded" title="Approve">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('reject', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded" title="Reject">
                <XCircle size={16} />
              </button>
            </>
          )}
          {job.status === 'rejected' && job.submitted_by && (
            <button onClick={() => onAction('resubmit', job.id)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded" title="Resubmit">
              <RefreshCw size={14} />
            </button>
          )}
          {job.status === 'scheduled' && (
            <button onClick={() => onAction('start', job.id)} className="p-1.5 text-print-400 hover:bg-print-900/50 rounded" title="Start Print">
              <Play size={16} />
            </button>
          )}"""

if old_actions in content:
    content = content.replace(old_actions, new_actions)
    changes += 1
    print("✓ Added approve/reject/resubmit buttons to JobRow")

# ============================================================
# 6. Add rejected reason display in JobRow item column
# ============================================================

old_notes = """        {job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}"""

new_notes = """        {job.rejected_reason && (
          <div className="text-xs text-red-400 flex items-center gap-1 truncate max-w-xs">
            <MessageSquare size={10} />
            {job.rejected_reason}
          </div>
        )}
        {job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}"""

if old_notes in content and "rejected_reason" not in content:
    content = content.replace(old_notes, new_notes)
    changes += 1
    print("✓ Added rejected reason display to JobRow")

# ============================================================
# 7. Add RejectModal component (before export default)
# ============================================================

reject_modal = '''
function RejectModal({ isOpen, onClose, onSubmit }) {
  const [reason, setReason] = useState('')
  if (!isOpen) return null
  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded-xl w-full max-w-md p-4 sm:p-6 border border-farm-700">
        <h2 className="text-lg font-display font-semibold mb-4">Reject Job</h2>
        <div className="mb-4">
          <label className="block text-sm text-farm-400 mb-1">Reason (required)</label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g., Too much filament. Please re-slice with 10% infill."
            className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
            rows={3}
            autoFocus
          />
        </div>
        <div className="flex justify-end gap-3">
          <button onClick={() => { onClose(); setReason('') }} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm">Cancel</button>
          <button
            onClick={() => { if (reason.trim()) { onSubmit(reason.trim()); setReason(''); onClose() } }}
            disabled={!reason.trim()}
            className="px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 rounded-lg text-sm text-white"
          >
            Reject
          </button>
        </div>
      </div>
    </div>
  )
}

'''

# Insert before "export default function Jobs()"
if "RejectModal" not in content:
    content = content.replace("export default function Jobs()", reject_modal + "export default function Jobs()")
    changes += 1
    print("✓ Added RejectModal component")

# ============================================================
# 8. Add approval mutations and state inside Jobs()
# ============================================================

old_state = """  const [jobTypeFilter, setJobTypeFilter] = useState('all')"""

new_state = """  const [jobTypeFilter, setJobTypeFilter] = useState('all')
  const [showRejectModal, setShowRejectModal] = useState(false)
  const [rejectingJobId, setRejectingJobId] = useState(null)

  const { data: approvalSetting } = useQuery({
    queryKey: ['approval-setting'],
    queryFn: getApprovalSetting,
  })"""

if old_state in content and "showRejectModal" not in content:
    content = content.replace(old_state, new_state)
    changes += 1
    print("✓ Added approval state and query")

# ============================================================
# 9. Add approval mutations (after repeatJob mutation)
# ============================================================

old_repeat_success = """    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] })
  })

  const handleAction = (action, jobId) => {"""

new_repeat_success = """    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] })
  })

  const approveJobMut = useMutation({
    mutationFn: approveJob,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const rejectJobMut = useMutation({
    mutationFn: ({ jobId, reason }) => rejectJob(jobId, reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const resubmitJobMut = useMutation({
    mutationFn: resubmitJob,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const handleAction = (action, jobId) => {"""

if old_repeat_success in content and "approveJobMut" not in content:
    content = content.replace(old_repeat_success, new_repeat_success)
    changes += 1
    print("✓ Added approval mutations")

# ============================================================
# 10. Add approve/reject/resubmit cases to handleAction
# ============================================================

old_handle = """      case 'delete': if (confirm('Delete this job?')) deleteJob.mutate(jobId); break
    }"""

new_handle = """      case 'delete': if (confirm('Delete this job?')) deleteJob.mutate(jobId); break
      case 'approve': approveJobMut.mutate(jobId); break
      case 'reject': setRejectingJobId(jobId); setShowRejectModal(true); break
      case 'resubmit': resubmitJobMut.mutate(jobId); break
    }"""

if old_handle in content and "'approve'" not in content:
    content = content.replace(old_handle, new_handle)
    changes += 1
    print("✓ Added approve/reject/resubmit to handleAction")

# ============================================================
# 11. Add approval tab to type filter logic
# ============================================================

old_filter = """  // Filter by job type (all, order, adhoc)
  const typeFilteredJobs = (jobsData || []).filter(job => {
    if (jobTypeFilter === 'order') return job.order_item_id != null
    if (jobTypeFilter === 'adhoc') return job.order_item_id == null
    return true
  })"""

new_filter = """  // Filter by job type (all, order, adhoc, approval)
  const typeFilteredJobs = (jobsData || []).filter(job => {
    if (jobTypeFilter === 'approval') return job.status === 'submitted'
    if (jobTypeFilter === 'order') return job.order_item_id != null
    if (jobTypeFilter === 'adhoc') return job.order_item_id == null
    return true
  })"""

if old_filter in content:
    content = content.replace(old_filter, new_filter)
    changes += 1
    print("✓ Added approval filter to type filtering logic")

# ============================================================
# 12. Add approval count to badge display
# ============================================================

old_counts = """  // Count jobs by type for badge display
  const orderJobCount = (jobsData || []).filter(j => j.order_item_id != null).length
  const adhocJobCount = (jobsData || []).filter(j => j.order_item_id == null).length"""

new_counts = """  // Count jobs by type for badge display
  const approvalJobCount = (jobsData || []).filter(j => j.status === 'submitted').length
  const orderJobCount = (jobsData || []).filter(j => j.order_item_id != null).length
  const adhocJobCount = (jobsData || []).filter(j => j.order_item_id == null).length"""

if old_counts in content and "approvalJobCount" not in content:
    content = content.replace(old_counts, new_counts)
    changes += 1
    print("✓ Added approvalJobCount")

# ============================================================
# 13. Update tab badge counts to include approval
# ============================================================

old_badge = """              {tab.value === 'all' ? (jobsData?.length || 0) :
               tab.value === 'order' ? orderJobCount : adhocJobCount}"""

new_badge = """              {tab.value === 'all' ? (jobsData?.length || 0) :
               tab.value === 'approval' ? approvalJobCount :
               tab.value === 'order' ? orderJobCount : adhocJobCount}"""

if old_badge in content and "'approval'" not in content:
    content = content.replace(old_badge, new_badge)
    changes += 1
    print("✓ Updated tab badge counts")

# ============================================================
# 14. Add RejectModal render at the end (before closing div)
# ============================================================

old_create_modal = """      <CreateJobModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={(data) => createJob.mutate(data)}
        modelsData={modelsData}
      />
    </div>
  )
}"""

new_create_modal = """      <CreateJobModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={(data) => createJob.mutate(data)}
        modelsData={modelsData}
      />
      <RejectModal
        isOpen={showRejectModal}
        onClose={() => { setShowRejectModal(false); setRejectingJobId(null) }}
        onSubmit={(reason) => rejectJobMut.mutate({ jobId: rejectingJobId, reason })}
      />
    </div>
  )
}"""

if old_create_modal in content and "RejectModal" not in content.split("export default")[1]:
    content = content.replace(old_create_modal, new_create_modal)
    changes += 1
    print("✓ Added RejectModal render")

# ============================================================
# 15. Hide approval tab when feature is disabled
# ============================================================
# We'll conditionally show the tab only if approvalSetting is enabled OR there are submitted jobs
# This is handled by just always showing it - submitted jobs count as the indicator

# Write
if changes > 0:
    with open(JOBS_JSX, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes to Jobs.jsx")
else:
    print("\n⚠ No changes applied.")
