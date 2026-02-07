"""
v0.17.0 Phase 3 Deploy Script — Dashboard Alerts Widget
Patches: Dashboard.jsx

Run from /opt/printfarm-scheduler/
    python3 deploy_v0170_alerts_phase3.py
"""
import os
import shutil

DASHBOARD_PATH = "/opt/printfarm-scheduler/frontend/src/pages/Dashboard.jsx"


def backup_file(filepath):
    bak = filepath + ".bak_v017"
    if not os.path.exists(bak):
        shutil.copy2(filepath, bak)
        print(f"  Backed up {os.path.basename(filepath)}")


def patch_dashboard():
    filepath = DASHBOARD_PATH
    backup_file(filepath)

    with open(filepath, "r") as f:
        content = f.read()

    if "AlertsWidget" in content:
        print("  Dashboard.jsx already has AlertsWidget — skipping")
        return

    # 1. Add import for alerts API and useNavigate
    content = content.replace(
        "import { stats, jobs, scheduler, printers, printJobs } from '../api'",
        "import { stats, jobs, scheduler, printers, printJobs, alerts as alertsApi } from '../api'"
    )

    content = content.replace(
        "import clsx from 'clsx'",
        "import { useNavigate } from 'react-router-dom'\nimport clsx from 'clsx'"
    )

    # 2. Add AlertsWidget component before the Dashboard export
    widget_component = '''
function AlertsWidget() {
  const navigate = useNavigate()
  const { data: summary } = useQuery({
    queryKey: ['alert-summary'],
    queryFn: alertsApi.summary,
    refetchInterval: 15000,
  })

  if (!summary || summary.total === 0) return null

  const items = [
    { key: 'print_failed', count: summary.print_failed, icon: '\\u{1F534}', label: 'failed print', plural: 'failed prints', filter: 'critical' },
    { key: 'spool_low', count: summary.spool_low, icon: '\\u{1F7E1}', label: 'low spool', plural: 'low spools', filter: 'warning' },
    { key: 'maintenance_overdue', count: summary.maintenance_overdue, icon: '\\u{1F7E1}', label: 'maintenance overdue', plural: 'maintenance overdue', filter: 'warning' },
  ].filter(i => i.count > 0)

  return (
    <div className="mb-6 md:mb-8 rounded-xl border border-amber-600/30 bg-amber-950/20 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <AlertTriangle size={16} className="text-amber-400" />
          <span className="text-sm font-semibold text-amber-300">Active Alerts</span>
        </div>
        <button
          onClick={() => navigate('/alerts')}
          className="text-xs text-amber-400 hover:text-amber-300 transition-colors"
        >
          View all \\u2192
        </button>
      </div>
      <div className="space-y-1.5">
        {items.map(item => (
          <button
            key={item.key}
            onClick={() => navigate(`/alerts?filter=${item.filter}`)}
            className="flex items-center gap-2 w-full text-left hover:bg-amber-900/20 rounded-lg px-2 py-1.5 transition-colors"
          >
            <span className="text-sm">{item.icon}</span>
            <span className="text-sm text-amber-200">
              {item.count} {item.count === 1 ? item.label : item.plural}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}

'''

    content = content.replace(
        "export default function Dashboard() {",
        widget_component + "export default function Dashboard() {"
    )

    # 3. Insert AlertsWidget into the dashboard layout (above stat cards)
    content = content.replace(
        '      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4 mb-6 md:mb-8">',
        '      <AlertsWidget />\n\n      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4 mb-6 md:mb-8">'
    )

    with open(filepath, "w") as f:
        f.write(content)

    print("  Dashboard.jsx patched with AlertsWidget")


def main():
    print("=" * 60)
    print("v0.17.0 Phase 3 — Dashboard Alerts Widget")
    print("=" * 60)
    print()

    print("[1/1] Patching Dashboard.jsx...")
    patch_dashboard()

    print()
    print("=" * 60)
    print("Done! Widget shows above stat cards when alerts exist.")
    print("  - Only visible when there are active (unread/undismissed) alerts")
    print("  - Shows count of failed prints, low spools, maintenance overdue")
    print("  - Click any line to jump to Alerts page filtered by type")
    print("  - Polls every 15 seconds")
    print("=" * 60)


if __name__ == "__main__":
    main()
