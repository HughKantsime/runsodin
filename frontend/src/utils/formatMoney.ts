export function formatMoney(amount: number, currency: string | null, precision?: number): string {
  const digits = currency === 'JPY' ? 0 : precision ?? 2
  return new Intl.NumberFormat(undefined, {
    ...(currency ? { style: 'currency', currency, currencyDisplay: 'code' } : {}),
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(amount)
}
