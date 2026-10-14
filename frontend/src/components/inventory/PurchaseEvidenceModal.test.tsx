import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import PurchaseEvidenceAction, { validPurchaseDecimal } from './PurchaseEvidenceModal'
import type { Spool, PurchaseEvidence } from '../../types/inventory'
const api = vi.hoisted(() => ({ update: vi.fn(), purchaseEvidence: vi.fn() }))
vi.mock('../../api/inventory', () => ({ spools: api }))
const fixture = { id: 7, purchase_evidence: { state: 'unknown', can_write: true, revision: 0 } } as Spool & { purchase_evidence: PurchaseEvidence }
function mount(spool = fixture, pendingRead?: Promise<unknown>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 1, role: 'admin' })
  if (pendingRead) api.purchaseEvidence.mockReturnValue(pendingRead)
  else api.purchaseEvidence.mockResolvedValue(spool.purchase_evidence)
  render(<QueryClientProvider client={client}><PurchaseEvidenceAction spool={spool} /></QueryClientProvider>); return client
}
function fill() {
  fireEvent.change(screen.getByLabelText('Purchase currency'), { target: { value: 'USD' } })
  fireEvent.change(screen.getByLabelText('Total purchase price'), { target: { value: '000.000' } })
  fireEvent.change(screen.getByLabelText('Purchased net filament weight (g)'), { target: { value: '001000.00' } })
}
beforeEach(() => { cleanup(); vi.clearAllMocks(); api.update.mockResolvedValue({ success: true }) })
describe('explicit purchase evidence', () => {
  it('validates plain bounded decimals without losing zero', () => {
    for (const v of ['1e3', '-1', '+1', ' 1', '1 ', '.5', '1.', 'NaN', 'Infinity']) expect(validPurchaseDecimal(v, false)).toBe(false)
    expect(validPurchaseDecimal('000.000', false)).toBe(true)
    expect(validPurchaseDecimal('000.000', true)).toBe(false)
    expect(validPurchaseDecimal('000.0010', true)).toBe(true)
  })
  it('hides unavailable and readonly owner actions', async () => {
    mount({ ...fixture, purchase_evidence: { state: 'unavailable', can_write: false } }); expect(screen.queryByText('Purchase evidence')).toBeNull()
    cleanup(); mount({ ...fixture, purchase_evidence: { state: 'unknown', can_write: false, revision: 0 } }); await screen.findByText('Purchase evidence unknown'); expect(screen.queryByText('Purchase evidence')).toBeNull()
  })
  it('starts blank and sends exact strings including zero', async () => {
    mount(); fireEvent.click(await screen.findByText('Purchase evidence'))
    expect(screen.getByLabelText('Purchase currency')).toHaveValue(''); expect(screen.getByLabelText('Purchased net filament weight (g)')).toHaveValue('')
    fill(); fireEvent.click(screen.getByText('Record purchase evidence'))
    await waitFor(() => expect(api.update).toHaveBeenCalledWith({ id: 7, expected_purchase_revision: 0, purchase_evidence: { currency: 'USD', price: '000.000', net_weight_g: '001000.00' } }))
  })
  it('requires reload and explicit reconfirm after stale CAS', async () => {
    api.update.mockRejectedValueOnce(new Error('Purchase evidence changed'))
    mount(); fireEvent.click(await screen.findByText('Purchase evidence')); fill(); fireEvent.click(screen.getByText('Record purchase evidence'))
    await screen.findByText('Reload latest evidence'); expect(api.update).toHaveBeenCalledTimes(1); expect(screen.getByText('Record purchase evidence')).toBeDisabled()
    api.purchaseEvidence.mockResolvedValue({ state: 'unknown', can_write: true, revision: 2 }); fireEvent.click(screen.getByText('Reload latest evidence')); await screen.findByText('Review the latest evidence and confirm your change again.'); expect(api.update).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('Record purchase evidence')); await waitFor(() => expect(api.update).toHaveBeenCalledTimes(2)); expect(api.update.mock.calls[1][0].expected_purchase_revision).toBe(2)
  })
  it('cache-only account switch removes private basis and discards old save completion', async () => {
    let resolve!: (value: unknown) => void
    api.update.mockReturnValue(new Promise(done => { resolve = done }))
    const client = mount(); fireEvent.click(await screen.findByText('Purchase evidence')); fill(); fireEvent.click(screen.getByText('Record purchase evidence'))
    api.purchaseEvidence.mockResolvedValue({ state: 'unavailable', can_write: false })
    await act(async () => client.setQueryData(['auth', 'me'], { id: 4, group_id: 2, role: 'viewer' }))
    await waitFor(() => expect(screen.queryByText('Total purchase price')).toBeNull())
    await act(async () => resolve({ success: true }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(client.getQueryData(['spools'])).toBeUndefined()
  })
  it('clears using revision and null without invented denomination', async () => {
    mount({ ...fixture, purchase_evidence: { state: 'recorded', can_write: true, revision: 3, evidence_nonce: 'a', currency: 'USD', price_decimal: '0', net_weight_g_decimal: '1000', weight_basis: 'explicit', provenance: 'purchase', recorded_at: 'now' } })
    fireEvent.click(await screen.findByText('Purchase evidence')); fireEvent.click(screen.getByText('Clear purchase evidence'))
    await waitFor(() => expect(api.update).toHaveBeenCalledWith({ id: 7, expected_purchase_revision: 3, purchase_evidence: null }))
  })
  it('pending save prevents duplicate writes and zero net is rejected', async () => {
    api.update.mockReturnValue(new Promise(() => {}))
    mount(); fireEvent.click(await screen.findByText('Purchase evidence')); fill()
    fireEvent.change(screen.getByLabelText('Purchased net filament weight (g)'), { target: { value: '0.000' } })
    fireEvent.click(screen.getByText('Record purchase evidence')); expect(api.update).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('positive net weight')
    fireEvent.change(screen.getByLabelText('Purchased net filament weight (g)'), { target: { value: '1000' } })
    fireEvent.click(screen.getByText('Record purchase evidence'))
    expect(screen.getByText('Record purchase evidence')).toBeDisabled()
    fireEvent.click(screen.getByText('Record purchase evidence')); expect(api.update).toHaveBeenCalledTimes(1)
  })
  it('late private read is discarded after cache-only logout', async () => {
    let resolve!: (value: unknown) => void
    const client = mount(fixture, new Promise(done => { resolve = done }))
    expect(screen.getByText('Purchase evidence loading…')).toBeInTheDocument()
    await act(async () => client.setQueryData(['auth', 'me'], null))
    await act(async () => resolve({ state: 'recorded', can_write: true, revision: 1, currency: 'EUR', price_decimal: '999', net_weight_g_decimal: '950' }))
    expect(screen.queryByText(/999/)).toBeNull(); expect(screen.queryByText('Purchase evidence')).toBeNull()
    expect(client.getQueryData(['spools'])).toBeUndefined()
  })
  it('read failure is visible with real refetch retry', async () => {
    const client = mount(); await screen.findByText('Purchase evidence')
    api.purchaseEvidence.mockRejectedValueOnce(new Error('Owned read unavailable'))
    await act(async () => client.invalidateQueries({ queryKey: ['spool-purchase'] }))
    await screen.findByText('Purchase evidence unavailable')
    fireEvent.click(screen.getByText('Retry purchase evidence'))
    await screen.findByText('Purchase evidence')
  })
  it('readonly explicit zero basis remains distinct from unknown', async () => {
    const client = mount({ ...fixture, purchase_evidence: { state: 'recorded', can_write: false, revision: 1, evidence_nonce: 'a', currency: 'USD', price_decimal: '0', net_weight_g_decimal: '1000', weight_basis: 'purchased_net_filament_g', provenance: 'owner_attestation', recorded_at: 'now' } })
    await screen.findByText('Purchase: USD 0 / 1000 g'); expect(screen.queryByText('Purchase evidence')).toBeNull()
    await act(async () => client.setQueryData(['auth', 'me'], { id: 3, role: 'admin' }))
    await waitFor(() => expect(screen.queryByText('Purchase: USD 0 / 1000 g')).toBeNull())
  })

  it.each(['lost response', 'stale CAS'])('close/reopen preserves reload requirement after %s', async failure => {
    let durableRevision = failure === 'stale CAS' ? 1 : 0
    let lost = failure === 'lost response'
    api.update.mockImplementation(async (payload: { expected_purchase_revision: number }) => {
      if (payload.expected_purchase_revision !== durableRevision) throw new Error('Purchase evidence changed')
      durableRevision += 1
      if (lost) { lost = false; throw new Error('Response lost after committed write') }
      return { success: true }
    })
    mount(); fireEvent.click(await screen.findByText('Purchase evidence')); fill()
    fireEvent.click(screen.getByText('Record purchase evidence'))
    await screen.findByText('Reload latest evidence')
    expect(durableRevision).toBe(1); expect(api.update).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(screen.getByText('Purchase evidence'))
    expect(screen.getByText('Record purchase evidence')).toBeDisabled()
    expect(screen.getByText('Reload latest evidence')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Record purchase evidence'))
    expect(api.update).toHaveBeenCalledTimes(1); expect(durableRevision).toBe(1)
    api.purchaseEvidence.mockImplementation(async () => ({ state: 'recorded', can_write: true, revision: durableRevision, evidence_nonce: 'a', currency: 'USD', price_decimal: '0', net_weight_g_decimal: '1000', weight_basis: 'purchased_net_filament_g', provenance: 'owner_attestation', recorded_at: 'now' }))
    fireEvent.click(screen.getByText('Reload latest evidence'))
    await screen.findByText('Review the latest evidence and confirm your change again.')
    expect(api.update).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('Record purchase evidence'))
    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(2))
    expect(api.update.mock.calls[1][0].expected_purchase_revision).toBe(1)
    expect(durableRevision).toBe(2)
  })

})
