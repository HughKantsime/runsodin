/** Mapping controls follow stored printer identity, never host or display name. */
export function supportsFilamentMapping(printer?: { api_type?: string | null; model?: string | null; machine_type?: string | null }) {
  if (printer?.api_type === 'bambu') return true
  return printer?.api_type === 'moonraker' && [printer.model, printer.machine_type].some(
    identity => /\bkobra[\s_-]*s1\b/i.test(identity || ''),
  )
}
