import { useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { spools } from '../../api/inventory'
import type { PurchaseEvidence, PurchaseInput, Spool } from '../../types/inventory'
import { Modal } from '../ui'

export const PURCHASE_CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'NZD'] as const
export function validPurchaseDecimal(value: string, positive: boolean): boolean {
  return /^[0-9]{1,18}(\.[0-9]{1,12})?$/.test(value) && (!positive || /[1-9]/.test(value))
}
export function PurchaseFields({ currency, price, weight, onChange }: {
  currency: string; price: string; weight: string;
  onChange: (field: 'currency' | 'price' | 'weight', value: string) => void
}) {
  const inputStyle = "w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
  return <fieldset className="space-y-2">
    <label className="block">Purchase currency<select className={inputStyle} required value={currency} onChange={e => onChange('currency', e.target.value)}>
      <option value="">Select currency</option>{PURCHASE_CURRENCIES.map(code => <option key={code}>{code}</option>)}
    </select></label>
    <label className="block">Total purchase price<input className={inputStyle} required inputMode="decimal" value={price} onChange={e => onChange('price', e.target.value)} /></label>
    <label className="block">Purchased net filament weight (g)<input className={inputStyle} required inputMode="decimal" value={weight} onChange={e => onChange('weight', e.target.value)} /></label>
    <p className="text-sm">Use the explicit purchase amount and net filament weight. Operational spool defaults are not purchase evidence.</p>
  </fieldset>
}
export type PurchasePrincipal = { id: number; group_id: number | null; role: string }
export function purchaseAudience(principal?: PurchasePrincipal | null): string | null {
  return principal && Number.isSafeInteger(principal.id) && principal.id > 0
    && (principal.group_id === null || (Number.isSafeInteger(principal.group_id) && principal.group_id > 0))
    && typeof principal.role === 'string' && principal.role.length > 0
    ? JSON.stringify([principal.id, principal.group_id, principal.role]) : null
}
export default function PurchaseEvidenceAction({ spool }: { spool: Spool }) {
  const { data: principal } = useQuery<PurchasePrincipal | null>({ queryKey: ['auth', 'me'], enabled: false })
  const owner = purchaseAudience(principal)
  return owner ? <PurchaseEditor key={owner} spool={spool} owner={owner} /> : null
}
function PurchaseEditor({ spool, owner }: { spool: Spool; owner: string }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const currentAudience = () => purchaseAudience(queryClient.getQueryData<PurchasePrincipal | null>(['auth', 'me'])) === owner
  const { data: evidence, refetch, isPending, isError } = useQuery<PurchaseEvidence>({
    queryKey: ['spool-purchase', owner, spool.id],
    queryFn: async () => {
      const value = await spools.purchaseEvidence(spool.id)
      if (!currentAudience()) throw new Error('Purchase audience changed')
      return value
    }, retry: false,
  })
  const [fields, setFields] = useState({ currency: '', price: '', weight: '' })
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [needsReload, setNeedsReload] = useState(false)
  if (!currentAudience()) return null
  if (isPending) return <p className="text-xs text-[var(--brand-text-muted)]">Purchase evidence loading…</p>
  if (isError) return <div className="text-xs text-[var(--brand-text-muted)]"><p>Purchase evidence unavailable</p><button type="button" onClick={() => { if (currentAudience()) void refetch() }}>Retry purchase evidence</button></div>
  if (!evidence || evidence.state === 'unavailable') return <p className="text-xs text-[var(--brand-text-muted)]">Purchase evidence unavailable</p>
  if (!evidence.can_write) return <p className="text-xs text-[var(--brand-text-muted)]">{evidence.state === 'recorded' ? `Purchase: ${evidence.currency} ${evidence.price_decimal} / ${evidence.net_weight_g_decimal} g` : 'Purchase evidence unknown'}</p>
  const save = async (purchase: PurchaseInput | null) => {
    if (!evidence || evidence.state === 'unavailable' || !evidence.can_write || pending || needsReload || !currentAudience()) return
    setPending(true); setError('')
    try {
      await spools.update({ id: spool.id, purchase_evidence: purchase, expected_purchase_revision: evidence.revision })
      if (!currentAudience()) return
      await queryClient.invalidateQueries({ queryKey: ['spools'] })
      if (!currentAudience()) return
      await refetch()
      if (currentAudience()) setOpen(false)
    } catch (cause) {
      if (!currentAudience()) return
      setError(cause instanceof Error ? cause.message : 'Purchase evidence could not be saved')
      setNeedsReload(true)
    } finally { setPending(false) }
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!PURCHASE_CURRENCIES.includes(fields.currency as PurchaseInput['currency']) || !validPurchaseDecimal(fields.price, false) || !validPurchaseDecimal(fields.weight, true)) {
      setError('Enter a supported currency, plain nonnegative price and positive net weight.'); return
    }
    void save({ currency: fields.currency as PurchaseInput['currency'], price: fields.price, net_weight_g: fields.weight })
  }
  const reload = async () => {
    setPending(true)
    try { const result = await refetch(); if (!currentAudience()) return; if (result.error) throw result.error; setNeedsReload(false); setError('Review the latest evidence and confirm your change again.') }
    catch { if (!currentAudience()) return; setError('Latest purchase evidence could not be loaded. Try reload again.') }
    finally { setPending(false) }
  }
  return <>
    <button className="text-xs text-[var(--brand-text-secondary)] border border-[var(--brand-card-border)] rounded-md px-2 py-1" type="button" onClick={() => { setOpen(true); if (!needsReload) setError('') }}>Purchase evidence</button>
    {open && <Modal isOpen onClose={() => { if (!pending) setOpen(false) }} title="Purchase evidence" size="md">
      <p>{evidence?.state === 'recorded' ? `${evidence.currency} ${evidence.price_decimal} / ${evidence.net_weight_g_decimal} g` : 'Purchase evidence unknown'}</p>
      <form onSubmit={submit} className="space-y-3">
        <PurchaseFields {...fields} onChange={(field, value) => setFields({ ...fields, [field]: value })} />
        {error && <p role="alert">{error}</p>}
        {needsReload && <button type="button" disabled={pending} onClick={() => void reload()}>Reload latest evidence</button>}
        <button type="submit" disabled={pending || needsReload || !evidence?.can_write}>Record purchase evidence</button>
        {evidence?.state === 'recorded' && <button type="button" disabled={pending || needsReload || !evidence.can_write} onClick={() => void save(null)}>Clear purchase evidence</button>}
      </form>
    </Modal>}
  </>
}
