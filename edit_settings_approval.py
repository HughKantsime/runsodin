#!/usr/bin/env python3
"""
Edit Settings.jsx to add job approval toggle in General tab.
Run on server: python3 edit_settings_approval.py
"""

SETTINGS_JSX = "/opt/printfarm-scheduler/frontend/src/pages/Settings.jsx"

with open(SETTINGS_JSX, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Add approval setting state and imports
# ============================================================

# Add import for getApprovalSetting, setApprovalSetting
# Find existing api imports
if "getApprovalSetting" not in content:
    # Look for the import from '../api' line
    import_lines = content.split("\n")
    for i, line in enumerate(import_lines):
        if "from '../api'" in line or 'from "../api"' in line:
            # Add after this line
            import_lines.insert(i + 1, "import { getApprovalSetting, setApprovalSetting } from '../api'")
            content = "\n".join(import_lines)
            changes += 1
            print("✓ Added approval API imports")
            break
    else:
        print("✗ Could not find api import line in Settings.jsx")

# ============================================================
# 2. Add approval toggle section before Interface Mode
# ============================================================

approval_section = """      {/* Job Approval Workflow */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <CheckCircle size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Job Approval Workflow</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          When enabled, viewer-role users (students) must have their print jobs approved by an operator or admin (teacher) before they can be scheduled. Operators and admins bypass approval.
        </p>
        <ApprovalToggle />
      </div>

      {/* Interface Mode */}"""

old_interface = """      {/* Interface Mode */}"""

if old_interface in content and "Job Approval Workflow" not in content:
    content = content.replace(old_interface, approval_section)
    changes += 1
    print("✓ Added approval workflow section to General tab")

# ============================================================
# 3. Add ApprovalToggle component before the Settings export
# ============================================================

approval_component = '''
function ApprovalToggle() {
  const queryClient = useQueryClient()
  const { data: setting, isLoading } = useQuery({
    queryKey: ['approval-setting'],
    queryFn: getApprovalSetting,
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled) => setApprovalSetting(enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['approval-setting'] }),
  })

  const enabled = setting?.require_job_approval || false

  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <div
        onClick={() => !isLoading && toggleMutation.mutate(!enabled)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          enabled ? 'bg-print-600' : 'bg-farm-700'
        }`}
      >
        <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
          enabled ? 'translate-x-[22px]' : 'translate-x-0.5'
        }`} />
      </div>
      <span className="text-sm">
        {enabled ? 'Approval required for viewer-role users' : 'Approval disabled — all users create jobs directly'}
      </span>
    </label>
  )
}

'''

# Find the right place to insert - before the main Settings export
if "ApprovalToggle" not in content:
    # Insert before "export default function Settings"
    export_marker = None
    for marker in ["export default function Settings", "export default function SettingsPage"]:
        if marker in content:
            export_marker = marker
            break
    
    if export_marker:
        content = content.replace(export_marker, approval_component + export_marker)
        changes += 1
        print("✓ Added ApprovalToggle component")
    else:
        print("✗ Could not find Settings export to insert ApprovalToggle before")

# ============================================================
# 4. Add useQueryClient import if not present
# ============================================================
# useQueryClient is likely already imported since Settings uses mutations
if "useQueryClient" not in content:
    content = content.replace(
        "from '@tanstack/react-query'",
        "useQueryClient, } from '@tanstack/react-query'"
    )
    print("· Added useQueryClient import")

# Write
if changes > 0:
    with open(SETTINGS_JSX, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes to Settings.jsx")
else:
    print("\n⚠ No changes applied.")
