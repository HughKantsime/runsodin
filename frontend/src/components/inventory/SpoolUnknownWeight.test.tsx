import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SpoolCard from './SpoolCard'
import SlotTile from '../foundry/SlotTile'
import { EditSpoolModal } from './SpoolEditModals'

vi.mock('./PurchaseEvidenceModal', () => ({ default: () => null }))
vi.mock('../../hooks/themePreferences', () => ({ useFoundry: () => false }))
vi.mock('../../permissions', () => ({ canDo: () => true }))

afterEach(() => cleanup())

const base: any = { id: 1, qr_code: 'SPL-1', status: 'active', filament_brand: 'Fixture', filament_name: 'PLA', filament_material: 'PLA',
  filament_color_hex: 'FF0000', initial_weight_g: 1000, remaining_weight_g: null, percent_remaining: 0, is_low_stock: true, spool_weight_g: 250 }
const noop = () => {}
const card = (spool: any) => render(<SpoolCard spool={spool} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />)

it('an unknown balance on a printer says it is not measured, never 0g', () => {
  const view = card({ ...base, location_printer_id: 5, location_slot: 2 })
  expect(screen.getByText('Not measured on this printer model')).toBeInTheDocument()
  expect(view.container).not.toHaveTextContent('0g')
  expect(screen.queryByRole('progressbar')).toBeNull()
})

it('an unknown balance in storage says not measured yet', () => {
  card({ ...base, location_printer_id: null, storage_location: 'Shelf A' })
  expect(screen.getByText('Not measured yet')).toBeInTheDocument()
})

it('a real zero is still shown as 0g', () => {
  card({ ...base, remaining_weight_g: 0, percent_remaining: 0 })
  expect(screen.getByText('0g / 1000g')).toBeInTheDocument()
})

it('slot tiles name a spool with no measured weight, and keep unknown for slots without a spool', () => {
  const { rerender } = render(<SlotTile slot={{ slot_number: 1, filament_type: 'PLA', assigned_spool_id: 7, remaining: null, remaining_weight: null } as any} />)
  expect(screen.getByText('Remaining not measured on this printer model')).toBeInTheDocument()
  rerender(<SlotTile slot={{ slot_number: 1, filament_type: 'PLA', remaining: null, remaining_weight: null } as any} />)
  expect(screen.getByText('Remaining unknown')).toBeInTheDocument()
})

it('the edit dialog sends a changed empty-spool weight and nothing else it was not given', () => {
  const onSave = vi.fn()
  const view = render(<EditSpoolModal spool={{ ...base, remaining_weight_g: 600 }} onClose={noop} onSave={onSave} />)
  fireEvent.change(screen.getByLabelText('Empty spool weight (g)'), { target: { value: '180' } })
  fireEvent.submit(view.container.ownerDocument.querySelector('form')!)
  const sent = onSave.mock.calls[0][0]
  expect(sent.spool_weight_g).toBe(180)
  expect(sent).not.toHaveProperty('remaining_weight_g')
})

it('an unchanged core weight is not sent, and an out-of-range one is refused', () => {
  const onSave = vi.fn()
  const view = render(<EditSpoolModal spool={{ ...base, remaining_weight_g: 600 }} onClose={noop} onSave={onSave} />)
  const form = view.container.ownerDocument.querySelector('form')!
  fireEvent.submit(form)
  expect(onSave.mock.calls[0][0]).not.toHaveProperty('spool_weight_g')
  fireEvent.change(screen.getByLabelText('Empty spool weight (g)'), { target: { value: '10001' } })
  fireEvent.submit(form)
  expect(onSave).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('alert')).toHaveTextContent('Empty-spool weight must be from 0 to 10000 g')
})

it('editing an unknown balance does not turn it into 0', () => {
  const onSave = vi.fn()
  const view = render(<EditSpoolModal spool={base} onClose={noop} onSave={onSave} />)
  expect(screen.getByLabelText('Remaining Weight (g)')).toHaveValue(null)
  fireEvent.change(screen.getByPlaceholderText('Any notes...'), { target: { value: 'note' } })
  fireEvent.submit(view.container.ownerDocument.querySelector('form')!)
  expect(onSave.mock.calls[0][0]).not.toHaveProperty('remaining_weight_g')
})
