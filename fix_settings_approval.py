#!/usr/bin/env python3
"""
Insert ApprovalToggle component into Settings.jsx.
Run on server: python3 fix_settings_approval.py
"""

SETTINGS_JSX = "/opt/printfarm-scheduler/frontend/src/pages/Settings.jsx"

with open(SETTINGS_JSX, "r") as f:
    content = f.read()

COMPONENT = '''
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
        {enabled ? 'Approval required for viewer-role users' : 'Approval disabled \\u2014 all users create jobs directly'}
      </span>
    </label>
  )
}

'''

if 'function ApprovalToggle' in content:
    print("· ApprovalToggle already exists in Settings.jsx")
else:
    # Insert before "export default function Settings()"
    content = content.replace(
        'export default function Settings()',
        COMPONENT + 'export default function Settings()'
    )
    with open(SETTINGS_JSX, "w") as f:
        f.write(content)
    print("✓ Inserted ApprovalToggle component into Settings.jsx")
