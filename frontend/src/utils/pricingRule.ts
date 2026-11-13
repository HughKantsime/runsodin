// One pricing rule for the Pricing Calculator's live preview. A line-for-line copy of
// backend/modules/models_library/pricing_rule.py; both are checked against the same table
// (tests/fixtures/pricing_rule_cases.json), so the page and the API cannot disagree.
//
// The saved "default_margin" has always been a MARKUP: price = cost x (1 + markup).

export interface PricingRuleConfig {
  hourly_rate?: number | null
  post_processing_min?: number | null
  packing_min?: number | null
  support_min?: number | null
  plate_handling_min?: number | null
  electricity_rate?: number | null
  printer_wattage?: number | null
  printer_cost?: number | null
  printer_lifespan: number
  packaging_cost?: number | null
  other_costs?: number | null
  failure_rate?: number | null
  monthly_rent?: number | null
  parts_per_month?: number | null
  default_margin?: number | null
  selling_fee_percent?: number | null
  selling_fee_fixed?: number | null
}

export interface PricedItem {
  material: number; labor: number; electricity: number; depreciation: number
  packaging: number; other: number; failure: number; overhead: number
  subtotal: number; markup_percent: number; price_before_fees: number
  selling_fees: number; price: number; margin_of_price_percent: number
}

const n = (value: number | null | undefined) => (value == null ? 0 : value)

export function priceItem(config: PricingRuleConfig, input: { material_cost: number; print_hours: number; units_per_plate?: number; markup_percent?: number | null }): PricedItem {
  const units = typeof input.units_per_plate === 'number' && input.units_per_plate >= 1 ? input.units_per_plate : 1
  const itemMinutes = n(config.post_processing_min) + n(config.packing_min) + n(config.support_min)
  const labor = (itemMinutes + n(config.plate_handling_min) / units) / 60 * n(config.hourly_rate)
  const electricity = (n(config.printer_wattage) / 1000) * input.print_hours * n(config.electricity_rate)
  const depreciation = (n(config.printer_cost) / config.printer_lifespan) * input.print_hours
  const packaging = n(config.packaging_cost)
  const other = n(config.other_costs)
  const base = input.material_cost + labor + electricity + depreciation + packaging + other
  const failure = base * (n(config.failure_rate) / 100)
  const parts = n(config.parts_per_month)
  const overhead = parts > 0 ? n(config.monthly_rent) / parts : 0
  const subtotal = base + failure + overhead

  const markup = input.markup_percent == null ? n(config.default_margin) : input.markup_percent
  const beforeFees = subtotal * (1 + markup / 100)
  const feePercent = n(config.selling_fee_percent)
  const feeFixed = n(config.selling_fee_fixed)
  if (feePercent >= 100) throw new RangeError('selling_fee_percent must be under 100')
  // The fee is a share of the selling price, so the price is solved for: after the fee is
  // taken, what is left still covers cost plus markup. With no fees this is beforeFees exactly.
  const price = feePercent || feeFixed ? (beforeFees + feeFixed) / (1 - feePercent / 100) : beforeFees
  const fees = price - beforeFees
  const marginOfPrice = price > 0 ? (price - fees - subtotal) / price * 100 : 0
  return {
    material: input.material_cost, labor, electricity, depreciation, packaging, other, failure, overhead,
    subtotal, markup_percent: markup, price_before_fees: beforeFees, selling_fees: fees, price,
    margin_of_price_percent: marginOfPrice,
  }
}

/** The same setting said the other way: a 50% markup is a 33.3% margin of the price. */
export function marginFromMarkup(markupPercent: number): number {
  return markupPercent <= -100 ? 0 : (markupPercent / (100 + markupPercent)) * 100
}

/** Margin must be under 100: a price can never be all profit. Returns null when it is not. */
export function markupFromMargin(marginPercent: number): number | null {
  if (!Number.isFinite(marginPercent) || marginPercent < 0 || marginPercent >= 100) return null
  return (marginPercent / (100 - marginPercent)) * 100
}
