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
export function pendingMessage(printer?: Printer) {
  const action = printer?.pending_control?.action
  return action && `${actionName(action)} sent, waiting for the printer`
}
export function controlLabel(printer: Printer | undefined, action: PrintControlAction) {
  return actionName(action) + (printer?.unproven_controls?.includes(action) ? ' (unproven)' : '')
}
export const unprovenHelp = 'ODIN has not yet proven this command on this printer model; watch the printer and use its touchscreen if nothing happens.'
export function unavailableMessage(action: PrintControlAction) {
  return `${actionName(action)} from ODIN does not work on this printer model. Use the printer's touchscreen.`
}
