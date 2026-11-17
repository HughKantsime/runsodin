import { useMutation, useQueryClient } from '@tanstack/react-query'
import CheckTarget from '../ui/CheckTarget'
import { getCurrentUser } from '../../permissions'
import { printerSupport } from '../../api/printerSupport'
import { experimentalWarning, isMonitorOnlyType, optInApplies, CAPABILITY_COPY, GATED_CAPABILITIES, supportLevelOfPrinter } from '../../utils/supportLevels'

type PrinterSupportFields = { id: number; api_type?: string | null; support_level?: unknown; experimental_actions_enabled?: boolean }

/** "Experimental" badge; nothing for a Supported printer. */
export function SupportLevelBadge({ printer }: { printer: PrinterSupportFields }) {
  if (supportLevelOfPrinter(printer) !== 'experimental') return null
  return (
    <span data-testid="support-badge" title="Experimental: not yet verified on real hardware"
      className="px-1.5 py-0.5 bg-amber-600/20 text-amber-400 text-[10px] rounded-full border border-amber-600/30 font-medium">Experimental</span>
  )
}

function OptInPanel({ printer, capability }: { printer: PrinterSupportFields; capability?: string }) {
  const client = useQueryClient()
  const principal = getCurrentUser()
  const isSuperadmin = principal?.role === 'admin' && !principal?.group_id
  const enabled = printer.experimental_actions_enabled === true
  const copy = capability ? CAPABILITY_COPY[capability] : null
  const checkboxId = `experimental-${printer.id}-${capability || 'type'}`
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
      <p role="note" className={copy ? "text-xs text-[var(--brand-text-secondary)]" : "text-xs text-amber-400"}>{copy ? copy.note : experimentalWarning(printer.api_type)}</p>
      <p className="text-xs text-[var(--brand-text-secondary)]">
        {copy ? copy.state(enabled) : `Monitoring is always on. Job sending and controls are ${enabled ? 'on' : 'off'} for this printer.`}
      </p>
      {isSuperadmin ? (
        <div className="flex items-center gap-2 min-h-11 text-xs">
          <CheckTarget touch><input id={checkboxId} type="checkbox" className="h-6 w-6 shrink-0" checked={enabled} disabled={save.isPending}
            aria-label={copy ? copy.label : 'Allow job sending and controls on this Experimental printer'}
            onChange={e => save.mutate(e.target.checked)} /></CheckTarget>
          <label htmlFor={checkboxId} className="flex items-center min-h-11 cursor-pointer">{copy ? copy.label : 'Allow job sending and controls'}</label>
        </div>
      ) : (
        <p className="text-xs text-[var(--brand-text-muted)]">Only a platform administrator can change this.</p>
      )}
      {save.isError && <p role="alert" className="text-xs text-red-400">{save.error.message}</p>}
    </div>
  )
}

/** Cards and page headers show whole-type warnings; the page disclosure opts into capability rows. */
export function ExperimentalOptIn({ printer, capabilities = false }: { printer: PrinterSupportFields; capabilities?: boolean }) {
  if (capabilities) {
    if (supportLevelOfPrinter(printer) !== 'supported' || !optInApplies(printer.api_type)) return null
    return <>{GATED_CAPABILITIES[(printer.api_type ?? '').trim().toLowerCase()]?.map(capability =>
      <OptInPanel key={capability} printer={printer} capability={capability} />)}</>
  }
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
