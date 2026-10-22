import { useEffect, useRef, useState } from 'react'
import { square, type SquareStatus, type SquareCatalog, type SquareStock } from '../../api/square'
import { getCurrentUser } from '../../permissions'

type Props = { products: { id: number; name: string }[] }
export default function SquareInventory({ products }: Props) {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<SquareStatus | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [token, setToken] = useState('')
  const [environment, setEnvironment] = useState('sandbox')
  const [locations, setLocations] = useState<{ id: string; name: string }[]>([])
  const [catalog, setCatalog] = useState<SquareCatalog | null>(null)
  const [product, setProduct] = useState('')
  const [location, setLocation] = useState('')
  const [variation, setVariation] = useState('')
  const [stock, setStock] = useState<Record<number, SquareStock>>({})
  const generation = useRef(0)
  const inFlight = useRef(false)
  const admin = open && getCurrentUser()?.role === 'admin'
  useEffect(() => () => { generation.current++ }, [])
  const clearDerived = () => { setCatalog(null); setLocations([]); setStock({}); setVariation(''); setLocation('') }
  const run = async (operation: () => Promise<void>) => {
    if (inFlight.current) return
    inFlight.current = true; setBusy(true); setError('')
    try { await operation() } catch (e: unknown) { setError(e instanceof Error ? e.message : 'Square could not be reached. Try again.'); setStock({}) }
    finally { inFlight.current = false; setBusy(false) }
  }
  const toggle = () => {
    if (busy) return
    generation.current++; setToken(''); setError(''); clearDerived(); setStatus(null)
    const next = !open; setOpen(next)
    if (next) { const g = generation.current; void run(async () => { const result = await square.status(); if (g === generation.current) { setStatus(result); setEnvironment(result.environment ?? 'sandbox') } }) }
  }
  const update = (result: SquareStatus) => { generation.current++; setStatus(result); setEnvironment(result.environment ?? 'sandbox'); clearDerived() }
  const connect = () => {
    const access_token = token; setToken('')
    void run(async () => update(await square.configure({ access_token, environment, expected_revision: status?.revision ?? null })))
  }
  const browse = () => {
    if (!status?.revision) return
    const revision = status.revision, g = generation.current
    void run(async () => {
      const [locs, items] = await Promise.all([square.locations(revision), square.catalog(revision)])
      if (g !== generation.current || locs.revision !== revision || items.revision !== revision) return
      setLocations(locs.locations); setCatalog(items)
    })
  }
  const more = () => {
    if (!catalog?.cursor || !status?.revision) return
    const revision = status.revision, g = generation.current, cursor = catalog.cursor
    void run(async () => { const page = await square.catalog(revision, cursor); if (g === generation.current && page.revision === revision) setCatalog(old => ({ ...page, items: [...(old?.items ?? []), ...page.items] })) })
  }
  const readStock = (id: number) => {
    if (!status?.revision) return
    const revision = status.revision, g = generation.current
    setStock(old => { const copy = { ...old }; delete copy[id]; return copy })
    void run(async () => { const result = await square.stock(id, revision); if (g === generation.current && result.revision === revision) setStock(old => ({ ...old, [id]: result })) })
  }
  const input = 'w-full min-w-0 rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)]'
  const button = 'px-3 py-2 rounded-md border border-[var(--brand-card-border)] disabled:opacity-50 text-sm'
  return <section className="mb-6 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] p-4">
    <button type="button" className="font-medium text-[var(--brand-primary)]" aria-expanded={open} onClick={toggle} disabled={busy}>Square inventory</button>
    {open && <div className="mt-3 space-y-4" aria-busy={busy}>
      <p className="text-sm text-[var(--brand-muted)]">Square is the source for finished stock. This panel reads inventory. Explicit inspected additions are sent separately from batch history when both activation keys are on. Printing and mapping do not change counts or publish listings.</p>
      {error && <p role="alert" className="text-sm text-red-400">{error} <button type="button" className={button} disabled={busy} onClick={() => { void run(async () => update(await square.status())) }}>Reload connection</button></p>}
      {busy && <p role="status" className="text-sm">Working…</p>}
      {status && <p className="text-sm">{status.connected ? `${status.environment === 'production' ? 'Production' : 'Sandbox'} account · ${status.merchant_id}. Identity verified; catalog and stock permissions are checked when read.` : 'Not connected.'}</p>}
      {admin && status && <form className="space-y-3" onSubmit={e => { e.preventDefault(); connect() }}>
        <label className="block text-sm">Square environment<select aria-label="Square environment" className={input} value={environment} disabled={busy} onChange={e => setEnvironment(e.target.value)}><option value="sandbox">Sandbox</option><option value="production">Production</option></select></label>
        <label className="block text-sm">Square access token<input aria-label="Square access token" className={input} type="password" autoComplete="off" value={token} disabled={busy} onChange={e => setToken(e.target.value)} /></label>
        <p className="text-xs text-[var(--brand-muted)]">Use your Square developer token with merchant, locations, catalog and inventory read access. ODIN encrypts it on this server and never displays it again. Changing account or environment clears local mappings.</p>
        <div className="flex flex-wrap gap-2"><button className={button} disabled={busy || !token.trim()} type="submit">{status.connected ? 'Verify and replace connection' : 'Verify and connect'}</button>
        {status.connected && <button className={button} type="button" disabled={busy} onClick={() => { setToken(''); void run(async () => update(await square.disconnect(status.revision!))) }}>Disconnect locally</button>}</div>
      </form>}
      {status?.connected && <>
        {admin && <div className="space-y-3 border-t border-[var(--brand-card-border)] pt-3">
          <button className={button} type="button" disabled={busy} onClick={browse}>Choose Square item</button>
          {catalog && <form className="space-y-3" onSubmit={e => { e.preventDefault(); void run(async () => update(await square.map(Number(product), { revision: status.revision!, location_id: location, variation_id: variation }))) }}>
            <label className="block text-sm">ODIN product<select aria-label="ODIN product" className={input} value={product} disabled={busy} onChange={e => setProduct(e.target.value)}><option value="">Choose product</option>{products.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
            <label className="block text-sm">Square location<select aria-label="Square location" className={input} value={location} disabled={busy} onChange={e => setLocation(e.target.value)}><option value="">Choose location</option>{locations.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}</select></label>
            <label className="block text-sm">Square variation<select aria-label="Square variation" className={input} value={variation} disabled={busy} onChange={e => setVariation(e.target.value)}><option value="">Choose variation</option>{catalog.items.map(v => <option key={v.variation_id} value={v.variation_id}>{v.item_name} / {v.variation_name}{v.sku ? ` (${v.sku})` : ''}</option>)}</select></label>
            <div className="flex flex-wrap gap-2">{catalog.cursor && <button className={button} type="button" disabled={busy} onClick={more}>Load more items</button>}<button className={button} type="submit" disabled={busy || !product || !location || !variation}>Save explicit mapping</button></div>
            <p className="text-xs text-[var(--brand-muted)]">ODIN verifies that inventory is tracked at this location. Matching a SKU alone never maps a product.</p>
          </form>}
        </div>}
        <div className="space-y-3">{status.mappings.length === 0 && <p className="text-sm">No products mapped yet.</p>}
          {status.mappings.map(m => <div key={m.product_id} className="rounded-md border border-[var(--brand-card-border)] p-3 space-y-2">
            <p className="break-words font-medium">{products.find(p => p.id === m.product_id)?.name ?? `Product ${m.product_id}`} → {m.item_name} / {m.variation_name}</p><p className="text-xs break-all text-[var(--brand-muted)]">Location: {m.location_id}</p>
            <div className="flex flex-wrap gap-2"><button type="button" className={button} disabled={busy} onClick={() => readStock(m.product_id)}>Read current stock</button>{admin && <button type="button" className={button} disabled={busy} onClick={() => { void run(async () => update(await square.unmap(m.product_id, status.revision!))) }}>Remove mapping</button>}</div>
            {stock[m.product_id] && <p className="text-sm" role="status">{stock[m.product_id].state === 'known' ? `Square in stock: ${stock[m.product_id].quantity}` : stock[m.product_id].state === 'untracked' ? 'Inventory is no longer tracked at this location.' : 'Square returned no count. Stock is unknown.'} <span className="text-xs text-[var(--brand-muted)]">Read {new Date(stock[m.product_id].retrieved_at).toLocaleString()}{stock[m.product_id].calculated_at ? ` · Calculated ${new Date(stock[m.product_id].calculated_at!).toLocaleString()}` : ''}</span></p>}
          </div>)}
        </div>
      </>}
    </div>}
  </section>
}
