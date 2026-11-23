import minorUnits from './currencyMinorUnits.json'

export function formatMoney(amount: unknown, currency: string | null, precision?: number | 'rate', fallback = 'Unknown'): string {
  const value = typeof amount === 'string' && amount.trim() ? Number(amount) : amount
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback

  const code = typeof currency === 'string' && Object.prototype.hasOwnProperty.call(minorUnits, currency) ? currency : null
  const units: number = code ? minorUnits[code] : 2
  let formatted: string
  if (precision === 'rate') {
    // Keep the stored rate, including significant digits smaller than a minor unit.
    const [whole, fraction = ''] = value.toLocaleString('en-US', { maximumSignificantDigits: 21 }).split('.')
    const decimals = fraction.padEnd(units, '0')
    formatted = decimals ? whole + '.' + decimals : whole
  } else {
    const digits = Number.isInteger(precision) && precision >= 0 && precision <= 20 ? precision : units
    // Calculator totals use the binary amount's toFixed rounding before display.
    formatted = Number(value.toFixed(digits)).toLocaleString('en-US', {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    })
  }
  return code ? code + ' ' + formatted : formatted
}
