import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { spools as spoolsApi } from '../../api'

/** Admin warning for duplicate Spoolman links (possible in 1.9.x data). Renders nothing for
 * non-admins (the endpoint answers 403) or when there is no conflict; links are never rewritten. */
export default function SpoolmanLinkConflicts() {
  const { data } = useQuery({ queryKey: ['spools', 'spoolman-link-conflicts'], queryFn: () => spoolsApi.linkConflicts(), retry: false })
  if (!Array.isArray(data) || data.length === 0) return null
  return (
    <div role="alert" className="mb-4 md:mb-6 p-3 md:p-4 bg-red-500/10 border border-red-500/30 rounded-md flex gap-3">
      <AlertTriangle className="text-red-400 shrink-0" size={20} aria-hidden="true" />
      <div className="text-sm text-red-200">
        <p className="font-medium">Spoolman spools linked to more than one ODIN spool — usage would be pushed twice. Unlink all but one:</p>
        <ul className="mt-1 list-disc pl-5">
          {data.map(conflict => (
            <li key={conflict.spoolman_spool_id}>
              Spoolman spool {conflict.spoolman_spool_id}: {conflict.spools.map(s => `#${s.id}${s.qr_code ? ` (${s.qr_code})` : ''}`).join(', ')}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
