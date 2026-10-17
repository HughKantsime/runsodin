import { Navigate } from 'react-router-dom'
import { canAccessPage } from '../../permissions'

interface RoleGateProps {
  page: string
  children: React.ReactNode
}

// Guards a route by its page-access key (Settings > Access > Permissions). A role without the
// key is sent to the dashboard; on the dashboard itself it sees a notice instead (no redirect loop).
export default function RoleGate({ page, children }: RoleGateProps) {
  if (!canAccessPage(page)) {
    if (page === 'dashboard') {
      return (
        <div className="p-6" role="alert">
          <h1 className="text-lg font-semibold">No dashboard access</h1>
          <p className="text-sm text-[var(--brand-text-secondary)]">Your role does not have access to the dashboard. Use the sidebar to open a page you can access.</p>
        </div>
      )
    }
    return <Navigate to="/" replace />
  }
  return children
}
