import { Navigate } from 'react-router-dom'
import { canAccessPage } from '../../permissions'

export default function RoleGate({ page, children }) {
  if (!canAccessPage(page)) {
    return <Navigate to="/" replace />
  }
  return children
}
