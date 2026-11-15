import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { replenishment, PrintNextList } from '../../api/replenishment'
import { canDo } from '../../permissions'
import { useLicense } from '../../LicenseContext'

/** Dashboard card: shown only when a product needs printing. Uses recorded counts; never asks Square. */
export default function PrintNextCard() {
  const allowed = useLicense().hasFeature('products') && canDo('products.assortment_preview')
  const [list, setList] = useState<PrintNextList | null>(null)
  useEffect(() => {
    let live = true
    if (allowed) replenishment.list(true, true).then(next => { if (live) setList(next) }).catch(() => { if (live) setList(null) })
    return () => { live = false }
  }, [allowed])
  if (!allowed || !list || list.work_hidden || !list.print_next_count) return null
  const top = list.rows.filter(row => row.rank).slice(0, 3)
  return <section aria-labelledby="print-next-card-title" className="mb-6 rounded-md border border-amber-500/60 bg-[var(--brand-card-bg)] p-4 text-sm">
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <h2 id="print-next-card-title" className="font-display text-base font-bold">Print next: {list.print_next_count} product{list.print_next_count === 1 ? '' : 's'} below {list.print_next_count === 1 ? 'its' : 'their'} reorder point</h2>
        {top.length > 0 && <ul className="mt-1 break-words">{top.map(row => <li key={row.product_id}>{row.product_name}: {row.on_hand?.quantity ?? '?'} on hand, {row.plates_needed} plate{row.plates_needed === 1 ? '' : 's'} suggested</li>)}</ul>}
      </div>
      <Link to="/products" className="inline-flex min-h-[44px] items-center justify-center rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-[var(--brand-primary)]">Open Print next</Link>
    </div>
  </section>
}
