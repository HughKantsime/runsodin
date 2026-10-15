// Cert C (F06-6.1-01, F06-6.6-01) — edit modals send the API's wire types for cost per gram and Spoolman link.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { EditFilamentModal, EditSpoolModal } from './SpoolEditModals'
import fixture from '../../test-fixtures/cert-C-spools-wire.json'

afterEach(() => cleanup())
const spool = (fixture.spools as any[])[0]

it('filament cost per gram is sent as a number, and blank as null', () => {
  const onSave = vi.fn()
  const view = render(<EditFilamentModal filament={{ id: 'lib_5', brand: 'Cert', name: 'ASA', material: 'ASA', color_hex: '', cost_per_gram: 0.031 }} onClose={() => {}} onSave={onSave} />)
  const cost = screen.getByPlaceholderText(/leave blank for global default/)
  expect(cost).toHaveValue(0.031)
  fireEvent.change(cost, { target: { value: '0.034' } })
  fireEvent.submit(view.container.ownerDocument.querySelector('form')!)
  expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'lib_5', cost_per_gram: 0.034 }))
  fireEvent.change(cost, { target: { value: '' } })
  fireEvent.submit(view.container.ownerDocument.querySelector('form')!)
  expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ cost_per_gram: null }))
})

it('spool edit links, rejects an invalid id, and unlinks a Spoolman spool', () => {
  const onSave = vi.fn()
  const view = render(<EditSpoolModal spool={{ ...spool, spoolman_spool_id: 42 }} onClose={() => {}} onSave={onSave} />)
  const field = screen.getByLabelText('Spoolman spool ID')
  expect(field).toHaveValue(42)
  const form = view.container.ownerDocument.querySelector('form')!
  fireEvent.change(field, { target: { value: '77' } })
  fireEvent.submit(form)
  expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ id: spool.id, spoolman_spool_id: 77 }))
  fireEvent.change(field, { target: { value: '0' } })
  fireEvent.submit(form)
  expect(onSave).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('alert')).toHaveTextContent('Spoolman spool ID must be a positive whole number')
  fireEvent.change(field, { target: { value: '' } })
  fireEvent.submit(form)
  expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ spoolman_spool_id: null }))
})

it('an unchanged (possibly duplicated) Spoolman link is not re-sent with other edits', () => {
  const onSave = vi.fn()
  const view = render(<EditSpoolModal spool={{ ...spool, spoolman_spool_id: 42 }} onClose={() => {}} onSave={onSave} />)
  fireEvent.change(screen.getByPlaceholderText('Any notes...'), { target: { value: 'edited' } })
  fireEvent.submit(view.container.ownerDocument.querySelector('form')!)
  expect(onSave).toHaveBeenCalledTimes(1)
  expect(onSave.mock.calls[0][0]).not.toHaveProperty('spoolman_spool_id')
  expect(onSave.mock.calls[0][0]).toMatchObject({ id: spool.id, notes: 'edited' })
})
