import { useCallback, useEffect, useRef, useState } from 'react'
import { replenishment, PrintNextList, PrintNextRow, QueueItem, QueueResult } from '../../api/replenishment'
import { canDo } from '../../permissions'

/** "3 minutes ago", "2 days ago": how old a stock count is. */
export function age(seconds: number): string {
  if (seconds < 90) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 90) return `${minutes} minutes ago`
  const hours = Math.round(minutes / 60)
  if (hours < 36) return `${hours} hours ago`
  return `${Math.round(hours / 24)} days ago`
}

const plates = (count: number) => `${count} plate${count === 1 ? '' : 's'}`
// Match QueueItem and QueueRequest limits; remaining demand is shown after reload.
const MAX_PLATES = 50, MAX_PRODUCTS = 100
const queueItem = (row: PrintNextRow): QueueItem => ({ product_id: row.product_id, recipe_id: row.recipe!.id, plates: Math.min((row.plates_needed ?? 0), MAX_PLATES) })
const label = (row: PrintNextRow) => row.status === 'ok' ? 'In stock' : row.status === 'blocked' ? 'Blocked' : (row.plates_needed ?? 0) > 0 ? 'Print' : 'Covered'
const tone: Record<string, string> = {
  Print: 'border-amber-500/60 text-amber-500', Covered: 'border-sky-500/60 text-sky-500',
  Blocked: 'border-red-500/60 text-red-400', 'In stock': 'border-emerald-500/60 text-emerald-500',
}
const button = 'min-h-[44px] w-full rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-sm disabled:opacity-50 sm:w-auto'

export default function PrintNext() {
  const allowed = canDo('products.assortment_preview')
  const [list, setList] = useState<PrintNextList | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [ready, setReady] = useState(false)
  const [result, setResult] = useState<QueueResult | null>(null), [canRefresh, setCanRefresh] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async (keepError = false) => {
    setBusy(true); if (!keepError) setError('')
    try {
      const next = await replenishment.list()
      setList(next)
      // Square is read at most once per interval: the button wakes when a new read is possible.
      if (timer.current) clearTimeout(timer.current)
      setCanRefresh(next.stock.refresh_in_seconds <= 0)
      if (next.stock.refresh_in_seconds > 0) timer.current = setTimeout(() => setCanRefresh(true), next.stock.refresh_in_seconds * 1000)
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not load the print-next list.') } finally { setBusy(false) }
  }, [])
  useEffect(() => { if (allowed) void load(); return () => { if (timer.current) clearTimeout(timer.current) } }, [allowed, load])

  const send = async (items: QueueItem[]) => {
    if (!items.length || busy) return
    setBusy(true); setError(''); setResult(null)
    let failed = false
    try { setResult(await replenishment.queue(items, ready)) } catch (e) { failed = true; setError(e instanceof Error ? e.message : 'Could not queue the plates.') }
    // Read the list again either way: queued plates now show as on the way. A queue error stays on screen.
    await load(failed)
  }

  if (!allowed) return null
  const rows = list?.rows ?? []
  const planning = list?.work_hidden === false && rows.every(r => 'shortfall' in r)
  const suggested = planning ? rows.filter(r => r.status === 'below' && (r.plates_needed ?? 0) > 0 && r.recipe) : []
  const all = suggested.slice(0, MAX_PRODUCTS).map(queueItem)
  const names = new Map(rows.map(r => [r.product_id, r.product_name]))
  const stock = list?.stock
  return <section aria-labelledby="print-next-title" aria-busy={busy} className="mb-6 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-4">
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h2 id="print-next-title" className="font-display text-lg font-bold">Print next</h2>
        <p className="text-sm text-[var(--brand-text-secondary)]">What to print, ranked from your stock counts and open orders. Queued plates wait on hold until you release them. Nothing is queued automatically and Square is never changed.</p>
      </div>
      <button type="button" className={button} disabled={busy || !canRefresh} onClick={() => void load()}>Refresh</button>
    </div>
    {list?.work_hidden && <p role="status" className="mt-3 text-sm break-words">Work in progress is hidden because this farm has restricted parts you are not cleared to see.</p>}
    {stock && <p className="mt-2 text-sm break-words" data-testid="print-next-stock">
      {stock.read_at ? `Stock read from Square ${age(stock.age_seconds ?? 0)} (${new Date(stock.read_at).toLocaleString()}).` : stock.connected ? 'No live stock read yet.' : 'Square is not connected.'}
      {' '}Counts are read at most every {Math.round(stock.cache_seconds / 60)} minutes{!canRefresh && stock.refresh_in_seconds > 0 ? `; the next read is possible in about ${stock.refresh_in_seconds} seconds` : ''}.
    </p>}
    {stock?.error && <p role="status" className="mt-2 rounded-md border border-amber-500/60 p-2 text-sm break-words">Square could not be read: {stock.error} Showing the last recorded counts with their age. Nothing is guessed.</p>}
    {error && <p role="alert" className="mt-2 text-sm text-red-400 break-words">{error}</p>}
    {planning && result && <div role="status" className="mt-2 rounded-md border border-[var(--brand-card-border)] p-2 text-sm break-words">
      {result.queued.map(q => <p key={q.product_id}>Queued {plates(q.plates)} for {names.get(q.product_id) ?? `product ${q.product_id}`}{q.held ? ', on hold. Release them from Jobs when ready.' : ', ready to schedule.'}</p>)}
      {result.skipped.map(s => <p key={s.product_id}>Not queued for {names.get(s.product_id) ?? `product ${s.product_id}`}: {s.reason}</p>)}
    </div>}
    {list && rows.length === 0 && <p className="mt-3 text-sm">No active products yet. Set a product to Active with a reorder point under “Show assortment and replenishment”.</p>}
    {list?.truncated && <p className="mt-2 text-sm">Only the first 1,000 products are checked.</p>}
    {suggested.length > 0 && <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <label className="flex min-h-[44px] items-center gap-2 text-sm"><input type="checkbox" checked={ready} disabled={busy} onChange={e => setReady(e.target.checked)} />Queue as ready (skip the hold)</label>
      <button type="button" className={button} disabled={busy} onClick={() => void send(all)}>{suggested.length > MAX_PRODUCTS ? `Queue next ${MAX_PRODUCTS} products` : 'Queue all suggested'} ({plates(all.reduce((sum, item) => sum + item.plates, 0))})</button>
    </div>}
    {suggested.length > MAX_PRODUCTS && <p className="mt-2 text-sm">Queue up to {MAX_PRODUCTS} products at a time. Remaining suggestions appear after the list reloads.</p>}
    {!list && busy && <p role="status" className="mt-3 text-sm">Reading stock…</p>}
    <ol className="mt-3 space-y-3" aria-label="Products, most urgent first">
      {rows.map(row => { const state = label(row); return <li key={row.product_id} className="rounded-md border border-[var(--brand-card-border)] p-3 text-sm break-words">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h3 className="font-medium">{row.rank ? `${row.rank}. ` : ''}{row.product_name} <span className={`ml-1 inline-block rounded border px-1.5 py-0.5 text-xs ${tone[state]}`}>{state}</span></h3>
            <p>{row.rank_reason ? `${row.rank_reason} ` : ''}{row.reason}</p>
          </div>
          {row.status === 'below' && (row.plates_needed ?? 0) > 0 && row.recipe && <button type="button" className={`${button} shrink-0 border-[var(--brand-primary)]`} disabled={busy} aria-label={`Queue ${plates(queueItem(row).plates)} for ${row.product_name}`}
            onClick={() => void send([queueItem(row)])}>Queue {plates(queueItem(row).plates)}</button>}
        </div>
        {row.recipe && (row.plates_needed ?? 0) > MAX_PLATES && <p className="mt-2">{plates((row.plates_needed ?? 0))} suggested; queue up to {MAX_PLATES} at a time. Remaining demand appears after the list reloads.</p>}
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-4">
          <div><dt className="text-xs text-[var(--brand-text-secondary)]">On hand</dt><dd>{row.on_hand ? `${row.on_hand.quantity} · ${row.on_hand.source === 'snapshot' ? 'last recorded ' : 'read '}${age(row.on_hand.age_seconds)}` : 'Unknown'}</dd></div>
          <div><dt className="text-xs text-[var(--brand-text-secondary)]">Print below / up to</dt><dd>{row.reorder_point} / {row.target}</dd></div>
          {row.on_the_way && <div><dt className="text-xs text-[var(--brand-text-secondary)]">On the way</dt><dd>{row.on_the_way.pieces}{row.on_the_way.awaiting_stock ? ` (${row.on_the_way.awaiting_stock} inspected, not yet in Square)` : ''}</dd></div>}
          {'shortfall' in row && <div><dt className="text-xs text-[var(--brand-text-secondary)]">Short</dt><dd>{row.shortfall ?? 'Unknown'}</dd></div>}
          {row.open_orders && <div><dt className="text-xs text-[var(--brand-text-secondary)]">Open orders</dt><dd>{row.open_orders.pieces} pieces</dd></div>}
          {row.velocity && <div><dt className="text-xs text-[var(--brand-text-secondary)]">Sold</dt><dd>{row.velocity.units_per_week} a week ({row.velocity.source}, last {row.velocity.weeks} weeks)</dd></div>}
        </dl>
        {row.recipe && <p className="mt-2">Plate: {row.recipe.name} · {row.recipe.pieces_per_plate} per plate · {row.recipe.hours_per_plate ? `about ${row.recipe.hours_per_plate} h per plate` : 'print time unknown'} · {row.recipe.materials}</p>}
      </li> })}
    </ol>
  </section>
}
