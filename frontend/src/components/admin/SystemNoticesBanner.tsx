import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'
import { systemNotices } from '../../api/system'
import { useOrg } from '../../contexts/OrgContext'

/** Dashboard banner for superadmins: active system notices (a quarantined restore). */
export default function SystemNoticesBanner() {
  const org = useOrg()
  const { data } = useQuery({
    queryKey: ['system-notices'],
    queryFn: systemNotices.list,
    enabled: org.isAdmin,
    refetchInterval: 60000,
  })
  if (!org.isAdmin) return null
  const notices = data?.notices || []
  if (notices.length === 0) return null
  return (
    <div className="mb-4 space-y-2">
      {notices.map((notice) => (
        <div
          key={`${notice.kind}:${notice.archive || ''}`}
          role="alert"
          className="flex items-start gap-3 p-3 rounded-md border bg-red-500/10 border-red-500/30"
        >
          <AlertTriangle size={18} className="text-red-400 shrink-0 mt-0.5" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-red-200">{notice.title}</p>
            <p className="text-xs text-red-300 mt-0.5">{notice.message}</p>
          </div>
          <Link to={notice.link} className="text-xs font-medium underline text-red-200 shrink-0">
            Open backups
          </Link>
        </div>
      ))}
    </div>
  )
}
