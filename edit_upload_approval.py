#!/usr/bin/env python3
"""
Edit Upload.jsx to show "Submit for Approval" instead of "Schedule Now"
when approval is enabled and user is a viewer.
Run on server: python3 edit_upload_approval.py
"""

UPLOAD_JSX = "/opt/printfarm-scheduler/frontend/src/pages/Upload.jsx"

with open(UPLOAD_JSX, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Add imports for approval setting query
# ============================================================

old_import = "import { printFiles } from '../api'"
new_import = """import { printFiles, getApprovalSetting } from '../api'
import { useQuery } from '@tanstack/react-query'"""

if old_import in content and "getApprovalSetting" not in content:
    content = content.replace(old_import, new_import)
    changes += 1
    print("✓ Added approval imports to Upload.jsx")

# ============================================================
# 2. Add approval query and role check in UploadSuccess
# ============================================================
# We need to make the Schedule Now button text dynamic.
# The simplest approach: pass a prop from the parent.

# Add approval query in the Upload component
old_upload_fn = """export default function Upload() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [uploadedFile, setUploadedFile] = useState(null)"""

new_upload_fn = """export default function Upload() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [uploadedFile, setUploadedFile] = useState(null)

  const { data: approvalSetting } = useQuery({
    queryKey: ['approval-setting'],
    queryFn: getApprovalSetting,
  })
  const approvalEnabled = approvalSetting?.require_job_approval || false
  // Check user role from localStorage token
  const userRole = (() => {
    try {
      const token = localStorage.getItem('token')
      if (!token) return null
      const payload = JSON.parse(atob(token.split('.')[1]))
      return payload.role
    } catch { return null }
  })()
  const showSubmitForApproval = approvalEnabled && userRole === 'viewer'"""

if old_upload_fn in content and "approvalSetting" not in content:
    content = content.replace(old_upload_fn, new_upload_fn)
    changes += 1
    print("✓ Added approval query to Upload component")

# ============================================================
# 3. Swap "Schedule Now" button text
# ============================================================

old_schedule_btn = """        <button onClick={onScheduleNow} className="flex items-center gap-2 px-4 py-2 rounded-lg bg-green-600 hover:bg-green-500 text-white font-medium transition-colors text-sm">
          <Calendar size={16} /> Schedule Now
        </button>"""

new_schedule_btn = """        <button onClick={onScheduleNow} className="flex items-center gap-2 px-4 py-2 rounded-lg bg-green-600 hover:bg-green-500 text-white font-medium transition-colors text-sm">
          <Calendar size={16} /> {submitForApproval ? 'Submit for Approval' : 'Schedule Now'}
        </button>"""

if old_schedule_btn in content and "submitForApproval" not in content:
    content = content.replace(old_schedule_btn, new_schedule_btn)
    changes += 1
    print("✓ Swapped Schedule Now button text")

# ============================================================
# 4. Pass submitForApproval prop to UploadSuccess
# ============================================================

old_success_render = """        <UploadSuccess 
          data={uploadedFile} 
          onUploadAnother={() => setUploadedFile(null)}
          onViewLibrary={() => navigate('/models')}
          onScheduleNow={() => navigate(`/models?schedule=${uploadedFile.model_id}`)}
        />"""

new_success_render = """        <UploadSuccess 
          data={uploadedFile} 
          onUploadAnother={() => setUploadedFile(null)}
          onViewLibrary={() => navigate('/models')}
          onScheduleNow={() => navigate(`/models?schedule=${uploadedFile.model_id}`)}
          submitForApproval={showSubmitForApproval}
        />"""

if old_success_render in content and "submitForApproval={" not in content:
    content = content.replace(old_success_render, new_success_render)
    changes += 1
    print("✓ Passed submitForApproval prop to UploadSuccess")

# ============================================================
# 5. Add submitForApproval to UploadSuccess function signature
# ============================================================

old_sig = "function UploadSuccess({ data, onUploadAnother, onViewLibrary, onScheduleNow, onUpdateObjects })"
new_sig = "function UploadSuccess({ data, onUploadAnother, onViewLibrary, onScheduleNow, onUpdateObjects, submitForApproval })"

if old_sig in content and "submitForApproval" not in content.split("function UploadSuccess")[1].split("{")[0]:
    content = content.replace(old_sig, new_sig)
    changes += 1
    print("✓ Added submitForApproval to UploadSuccess props")

# Write
if changes > 0:
    with open(UPLOAD_JSX, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes to Upload.jsx")
else:
    print("\n⚠ No changes applied.")
