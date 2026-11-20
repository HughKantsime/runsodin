import { useEffect, useReducer } from 'react'
import type { Printer, PrintControlAction } from '../types/printer'

export type ControlReceipt = {
  success?: boolean
  outcome?: 'confirmed' | 'pending'
  message?: string
  unproven?: boolean
  pending_control?: Printer['pending_control']
}

export function actionName(action: PrintControlAction) {
  return action.charAt(0).toUpperCase() + action.slice(1)
}
export function activeControl(printer?: Printer) {
  const record = printer?.pending_control
  return record && Date.parse(record.deadline_at) > Date.now() ? record : null
}

export function useControlDeadlines(printers: Printer[] = []) {
  const [, refresh] = useReducer((tick: number) => tick + 1, 0)
  const now = Date.now()
  let nextDeadline = Infinity
  for (const printer of printers) {
    if (!printer.pending_control) continue
    const deadline = Date.parse(printer.pending_control.deadline_at)
    if (deadline > now) nextDeadline = Math.min(nextDeadline, deadline)
  }
  useEffect(() => {
    if (!Number.isFinite(nextDeadline)) return
    const timer = setTimeout(refresh, Math.max(0, nextDeadline - Date.now()))
    return () => clearTimeout(timer)
  }, [nextDeadline])
}

export function pendingMessage(printer?: Printer) {
  const action = activeControl(printer)?.action
  return action && `${actionName(action)} sent, waiting for the printer`
}
export function controlLabel(printer: Printer | undefined, action: PrintControlAction) {
  return actionName(action) + (printer?.unproven_controls?.includes(action) ? ' (unproven)' : '')
}
export const unprovenHelp = 'ODIN has not yet proven this command on this printer model; watch the printer and use its touchscreen if nothing happens.'
export function unavailableMessage(action: PrintControlAction) {
  return `${actionName(action)} from ODIN does not work on this printer model. Use the printer's touchscreen.`
}
