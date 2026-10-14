import { canDo, getCurrentUser } from '../permissions'
export function canControlPlug(target: { org_id?: number | null; plug_type?: string | null }) {
  const user = getCurrentUser()
  if (!user || !['operator', 'admin'].includes(user.role) || !canDo('printers.edit') || !target.plug_type || target.org_id === undefined) return false
  return (user.role === 'admin' && !user.group_id) || target.org_id === null || target.org_id === user.group_id
}
