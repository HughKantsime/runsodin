import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, Clock } from 'lucide-react'
import { systemNotices, type SystemNotice } from '../../api/system'
import { useOrg } from '../../contexts/OrgContext'

const LINK_LABEL: Record<SystemNotice['kind'], string> = {
  restore_quarantined: 'Open backups',
  license_trial_ending: 'Open license',
  license_expiring: 'Open license',
}

/** Dashboard banner for superadmins: active system notices (a quarantined restore, a license expiring, a Pro trial ending). */
export default function SystemNoticesBanner() {
  const org = useOrg()
  const queryClient = useQueryClient()
  const [dismissing, setDismissing] = useState<string | null>(null)
  const { data } = useQuery({
    queryKey: ['system-notices'],
    queryFn: systemNotices.list,
    enabled: org.isAdmin,
    refetchInterval: 60000,
  })
  if (!org.isAdmin) return null
  const notices = data?.notices || []
  if (notices.length === 0) return null

  const dismiss = async (notice: SystemNotice) => {
    setDismissing(notice.kind)
    try {
      await systemNotices.dismiss(notice.kind)
      queryClient.setQueryData<{ notices: SystemNotice[] }>(['system-notices'], (old) =>
        old ? { notices: old.notices.filter((n) => n.kind !== notice.kind) } : old)
    } catch {
      // The notice stays visible; the next refresh shows the server's state.
    } finally {
      setDismissing(null)
      queryClient.invalidateQueries({ queryKey: ['system-notices'] })
    }
  }

  return (
    <div className="mb-4 space-y-2">
      {notices.map((notice) => {
        const warning = notice.severity === 'warning'
        const Icon = warning ? Clock : AlertTriangle
        return (
          <div
            key={`${notice.kind}:${notice.archive || ''}`}
            role="alert"
            className={`flex items-start gap-3 p-3 rounded-md border ${warning ? 'bg-amber-500/10 border-amber-500/30' : 'bg-red-500/10 border-red-500/30'}`}
          >
            <Icon size={18} className={`${warning ? 'text-amber-400' : 'text-red-400'} shrink-0 mt-0.5`} />
            <div className="min-w-0 flex-1">
              <p className={`text-sm font-medium ${warning ? 'text-amber-200' : 'text-red-200'}`}>{notice.title}</p>
              <p className={`text-xs mt-0.5 ${warning ? 'text-amber-300' : 'text-red-300'}`}>{notice.message}</p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              {notice.buy_url && (
                <a href={notice.buy_url} target="_blank" rel="noopener noreferrer" className="text-xs font-medium underline text-amber-200">
                  {notice.kind === 'license_trial_ending' ? 'Buy Pro' : 'Renew license'}
                </a>
              )}
              <Link to={notice.link} className={`text-xs font-medium underline ${warning ? 'text-amber-200' : 'text-red-200'}`}>
                {LINK_LABEL[notice.kind] ?? 'Open'}
              </Link>
              {notice.dismissible && (
                <button
                  type="button"
                  onClick={() => dismiss(notice)}
                  disabled={dismissing === notice.kind}
                  className="text-xs font-medium text-amber-200 hover:text-amber-100 disabled:opacity-50"
                >
                  Dismiss
                </button>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
