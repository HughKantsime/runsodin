// Cert C (F05-5.2/5.3) — real Models page wired to captured /models-with-pricing and /models/{id}/revisions.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Models from './Models'
import fixture from '../../test-fixtures/cert-C-models-wire.json'
import revisions from '../../test-fixtures/cert-C-model-revisions-wire.json'
const api = vi.hoisted(() => ({ list: vi.fn(), update: vi.fn(), revisions: vi.fn(), remove: vi.fn() }))
vi.mock('../../api', () => ({
  models: { listWithPricing: api.list, update: api.update, schedule: vi.fn(), getVariants: vi.fn(), scheduleOptions: vi.fn(), delete: api.remove },
  modelRevisions: { list: api.revisions, create: vi.fn(), revert: vi.fn() },
  filaments: { combined: vi.fn().mockResolvedValue([]) }, printers: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 3, role: 'admin' }), canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
beforeEach(() => { api.list.mockResolvedValue(fixture.models); api.update.mockResolvedValue({}); api.revisions.mockResolvedValue(revisions.revisions) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/models']}><Models /></MemoryRouter></QueryClientProvider>)
}
const names = () => ['Cert bracket', 'Cert hinge'].filter(name => screen.queryAllByText(name).length > 0)

test('F05-5.2-01 search filters the real library by name and by material', async () => {
  mount(); await screen.findAllByText('Cert hinge')
  fireEvent.change(screen.getByPlaceholderText('Search models...'), { target: { value: 'bracket' } })
  expect(names()).toEqual(['Cert bracket'])
  fireEvent.change(screen.getByPlaceholderText('Search models...'), { target: { value: 'petg' } })
  expect(names()).toEqual(['Cert hinge'])
})

test('F05-5.2-02 favourites filter and star toggle use the real favourite flags', async () => {
  mount(); await screen.findAllByText('Cert hinge')
  fireEvent.click(screen.getByRole('button', { name: 'Favorites (1)' }))
  expect(names()).toEqual(['Cert hinge'])
  fireEvent.click(screen.getByRole('button', { name: 'Favorites (1)' }))
  fireEvent.click(screen.getAllByRole('button', { name: 'Add to favorites' })[0])
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(fixture.models[0].id, { is_favorite: true }))
})

test('F05-5.3-01/03 History button opens the revision panel with numbers, notes and uploader', async () => {
  mount(); await screen.findAllByText('Cert hinge')
  fireEvent.click(screen.getAllByRole('button', { name: 'View revisions' })[0])
  const dialog = await screen.findByRole('dialog', { name: /Revisions/ })
  expect(await within(dialog).findByText('Cert new hinge')).toBeInTheDocument()
  for (const rev of revisions.revisions) expect(within(dialog).getByText(`v${rev.revision_number}`)).toBeInTheDocument()
  expect(within(dialog).getAllByText('by admin').length).toBe(revisions.revisions.length)
  expect(api.revisions).toHaveBeenCalledWith(fixture.models[0].id)
})

test('F05-5.2-03 stat cards from the real response: total models, print hours, most printed', async () => {
  mount(); await screen.findAllByText('Cert hinge')
  const card = (label: string) => screen.getAllByText(label, { exact: true }).at(-1)!.parentElement!.parentElement!
  expect(card('Models')).toHaveTextContent('2')
  expect(card('Total Print Hours')).toHaveTextContent('0.0h')
  expect(card('Most Printed')).toHaveTextContent('None')
})

test('F05-5.2-04 edit action on a real model card opens its stored metadata', async () => {
  mount(); await screen.findAllByText('Cert hinge')
  fireEvent.click(screen.getAllByRole('button', { name: 'Edit model' })[0])
  expect(await screen.findByDisplayValue('Cert bracket')).toBeInTheDocument()
})

test('F05-5.2-04 delete action on a real model card deletes that model after confirmation', async () => {
  api.remove.mockResolvedValue(undefined)
  mount(); await screen.findAllByText('Cert hinge')
  fireEvent.click(screen.getAllByRole('button', { name: 'Delete model' })[0])
  const confirm = await screen.findAllByRole('button', { name: 'Delete Model' })
  fireEvent.click(confirm[confirm.length - 1])
  await waitFor(() => expect(api.remove).toHaveBeenCalled())
  expect(api.remove.mock.calls[0][0]).toBe(fixture.models[0].id)
})
