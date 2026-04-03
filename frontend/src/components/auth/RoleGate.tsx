import { Navigate } from 'react-router-dom'
import { canAccessPage } from '../../permissions'

interface RoleGateProps {
  page: string
  children: React.ReactNode
}

export default function RoleGate({ page, children }: RoleGateProps) {
  if (!canAccessPage(page)) {
    return <Navigate to="/" replace />
  }
  return children
}
