import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getCurrentUser } from '../../permissions'
import { printerSupport } from '../../api/printerSupport'
import { experimentalWarning, isMonitorOnlyType, supportLevelOfPrinter } from '../../utils/supportLevels'

type PrinterSupportFields = { id: number; api_type?: string | null; support_level?: unknown; experimental_actions_enabled?: boolean }

/** "Experimental" badge; nothing for a Supported printer. */
export function SupportLevelBadge({ printer }: { printer: PrinterSupportFields }) {
  if (supportLevelOfPrinter(printer) !== 'experimental') return null
  return (
    <span data-testid="support-badge" title="Experimental: not yet verified on real hardware"
      className="px-1.5 py-0.5 bg-amber-600/20 text-amber-400 text-[10px] rounded-full border border-amber-600/30 font-medium">Experimental</span>
  )
}

function OptInPanel({ printer }: { printer: PrinterSupportFields }) {
  const client = useQueryClient()
  const principal = getCurrentUser()
  const isSuperadmin = principal?.role === 'admin' && !principal?.group_id
  const enabled = printer.experimental_actions_enabled === true
  const save = useMutation({
    mutationFn: (next: boolean) => printerSupport.setExperimentalActions(printer.id, next),
    retry: false,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['printers'] })
      client.invalidateQueries({ queryKey: ['printer', String(printer.id)] })
    },
  })
  return (
    <div data-testid="experimental-opt-in" className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)] space-y-1">
      <p role="note" className="text-xs text-amber-400">{experimentalWarning(printer.api_type)}</p>
      <p className="text-xs text-[var(--brand-text-secondary)]">
        Monitoring is always on. Job sending and controls are {enabled ? 'on' : 'off'} for this printer.
      </p>
      {isSuperadmin ? (
        <label className="flex items-center gap-2 text-xs cursor-pointer">
          <input type="checkbox" checked={enabled} disabled={save.isPending}
            aria-label="Allow job sending and controls on this Experimental printer"
            onChange={e => save.mutate(e.target.checked)} />
          Allow job sending and controls
        </label>
      ) : (
        <p className="text-xs text-[var(--brand-text-muted)]">Only a platform administrator can change this.</p>
      )}
      {save.isError && <p role="alert" className="text-xs text-red-400">{save.error.message}</p>}
    </div>
  )
}

/** Warning and opt-in toggle. Renders nothing (and uses no query client) unless the printer is Experimental. */
export function ExperimentalOptIn({ printer }: { printer: PrinterSupportFields }) {
  if (supportLevelOfPrinter(printer) !== 'experimental') return null
  if (isMonitorOnlyType(printer.api_type)) {
    return (
      <div data-testid="experimental-monitor-only" className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)] space-y-1">
        <p role="note" className="text-xs text-amber-400">{experimentalWarning(printer.api_type)}</p>
        <p className="text-xs text-[var(--brand-text-secondary)]">Monitoring only for now. Job sending and controls are not available for this printer type yet.</p>
      </div>
    )
  }
  return <OptInPanel printer={printer} />
}
