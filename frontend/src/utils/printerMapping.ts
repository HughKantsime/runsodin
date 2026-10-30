type PrinterIdentity = { api_type?: string | null; model?: string | null; machine_type?: string | null }

/** Follows stored printer identity, never host or display name. Same rule as the backend's is_kobra_s1. */
export function isKobraS1(printer?: PrinterIdentity) {
  return printer?.api_type === 'moonraker' && [printer.model, printer.machine_type].some(
    identity => /\bkobra[\s_-]*s1(?![a-z0-9])(?![\s_-]*max(?![a-z0-9]))/i.test(identity || ''),
  )
}

/** Mapping controls appear only where a stored mapping decides which slot feeds. */
export function supportsFilamentMapping(printer?: PrinterIdentity) {
  return printer?.api_type === 'bambu' || isKobraS1(printer)
}
