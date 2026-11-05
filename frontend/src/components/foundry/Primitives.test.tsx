import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { applyTheme } from '../../hooks/themePreferences'
import { Button, Input, Select, Textarea, Card, StatCard, Modal, EmptyState, TabBar, SearchInput, Table, PageState } from '../ui'
import Toast from '../ui/Toast'
import PrinterNumber from '../ui/PrinterNumber'
import FoundryPage from './Page'

const toaster = vi.hoisted(() => ({ props: null as any }))
vi.mock('react-hot-toast', () => ({ Toaster: (props: any) => { toaster.props = props; return null } }))
afterEach(() => { cleanup(); applyTheme('dark', 'classic') })

it.each(['primary', 'secondary', 'danger', 'ghost'] as const)('Foundry %s action preserves its handler, ref and disabled/loading semantics', variant => {
  applyTheme('dark', 'foundry')
  const action = vi.fn(), ref = createRef<HTMLButtonElement>()
  const { rerender } = render(<Button ref={ref} variant={variant} onClick={action}>Action</Button>)
  expect(ref.current).toHaveAttribute('data-foundry-variant', variant)
  fireEvent.click(screen.getByRole('button')); expect(action).toHaveBeenCalledTimes(1)
  rerender(<Button variant={variant} loading onClick={action}>Action</Button>)
  expect(screen.getByRole('button')).toBeDisabled()
  fireEvent.click(screen.getByRole('button')); expect(action).toHaveBeenCalledTimes(1)
})

it('inputs, selects and textarea retain labels, validation associations, refs and native events', () => {
  applyTheme('dark', 'foundry')
  const change = vi.fn(), ref = createRef<HTMLInputElement>()
  render(<><Input ref={ref} label="Name" error="Name required" onChange={change} /><Select label="Printer" error="Choose a printer" options={[{ value: '', label: 'Choose' }, { value: '1', label: 'Press' }]} onChange={change} /><Textarea label="Notes" /></>)
  for (const name of ['Name', 'Printer', 'Notes']) expect(screen.getByLabelText(name)).toHaveAttribute('data-foundry-ui', 'control')
  expect(ref.current).toHaveAccessibleDescription('Name required')
  expect(screen.getByLabelText('Printer')).toHaveAccessibleDescription('Choose a printer')
  fireEvent.change(ref.current!, { target: { value: 'Press' } })
  fireEvent.change(screen.getByLabelText('Printer'), { target: { value: '1' } })
  expect(change).toHaveBeenCalledTimes(2)
})

it('cards, empty state, native tables and searches receive Foundry variants', () => {
  applyTheme('dark', 'foundry')
  const { container } = render(<><Card>Inventory</Card><StatCard label="Hours" value={12} /><EmptyState title="No models" description="Upload a model" /><Table className="existing"><caption>Jobs</caption><tbody><tr><td>Bracket</td></tr></tbody></Table><SearchInput value="" onChange={() => {}} /></>)
  expect(container.querySelectorAll('[data-foundry-ui="card"]')).toHaveLength(2)
  expect(container.querySelector('[data-foundry-ui="empty"]')).toHaveTextContent('Upload a model')
  expect(screen.getByRole('table', { name: 'Jobs' })).toHaveAttribute('data-foundry-ui', 'table')
  expect(container.querySelector('[data-foundry-ui="search"]')).toBeTruthy()
})

it.each(['loading', 'error', 'empty'] as const)('%s state preserves content and real retry actions', kind => {
  applyTheme('dark', 'foundry')
  const retry = vi.fn()
  render(<PageState kind={kind}>Server message<button onClick={retry}>Retry</button></PageState>)
  const el = screen.getByRole(kind === 'error' ? 'alert' : 'status')
  expect(el).toHaveAttribute('data-foundry-ui', kind)
  if (kind === 'loading') expect(el).toHaveAttribute('aria-busy', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' })); expect(retry).toHaveBeenCalledOnce()
})

it('Foundry tabs expose their selected state and retain callbacks', () => {
  applyTheme('dark', 'foundry')
  const change = vi.fn()
  render(<TabBar tabs={['General', 'Access']} active="General" onChange={change} />)
  expect(screen.getByRole('button', { name: 'General' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Access' })); expect(change).toHaveBeenCalledWith('Access')
})

it('Foundry modal retains its accessible name and Escape handling', () => {
  applyTheme('dark', 'foundry')
  const close = vi.fn()
  render(<Modal isOpen title="Edit" onClose={close}><Input label="Name" /></Modal>)
  expect(screen.getByRole('dialog', { name: 'Edit' }).querySelector('[data-foundry-ui="modal"]')).toBeTruthy()
  fireEvent.keyDown(document, { key: 'Escape' }); expect(close).toHaveBeenCalledOnce()
})

it('toast options preserve the Classic surface and switch only Foundry presentation', () => {
  const { rerender } = render(<Toast />)
  expect(toaster.props).toEqual({ position: 'top-right', toastOptions: { style: { background: 'var(--brand-card-bg)', color: 'var(--brand-text-primary)', border: '1px solid var(--brand-card-border)' } } })
  act(() => applyTheme('dark', 'foundry')); rerender(<Toast />)
  expect(toaster.props.toastOptions.className).toBe('f-toast')
})

it.each([['dark', 'classic'], ['light', 'foundry'], ['high-contrast', 'foundry']] as const)('%s/%s emits no Foundry attributes or page wrappers', (mode, family) => {
  applyTheme(mode, family)
  const { container } = render(<FoundryPage name="/jobs"><Card className="original"><Button>Save</Button><Input label="Name" /><Select label="State" /><Textarea label="Notes" /><TabBar tabs={['All']} active="All" /><EmptyState title="Empty" /><PageState kind="loading" className="legacy">Loading</PageState><Table className="original-table"><tbody><tr><td>Data</td></tr></tbody></Table></Card></FoundryPage>)
  expect(container.querySelector('[data-foundry-page], [data-foundry-ui], [data-foundry-variant], [data-foundry-active]')).toBeNull()
  expect(container.firstElementChild).toHaveClass('original')
  expect(screen.getByText('Loading').outerHTML).toBe('<div class="legacy">Loading</div>')
  expect(screen.getByRole('table').outerHTML).toBe('<table class="original-table"><tbody><tr><td>Data</td></tr></tbody></table>')
})

it('numeric presentation keeps reported values and unknowns unchanged in either family', async () => {
  const { container, rerender } = render(<PrinterNumber as="p" className="legacy">Unknown temperature</PrinterNumber>)
  expect(container.innerHTML).toBe('<p class="legacy">Unknown temperature</p>')
  act(() => applyTheme('dark', 'foundry'))
  rerender(<PrinterNumber as="p" className="legacy">221°C</PrinterNumber>)
  expect(container.firstElementChild).toHaveAttribute('data-foundry-number', '')
  expect(container.firstElementChild).toHaveTextContent('221°C')
  await act(async () => applyTheme('dark', 'classic'))
  expect(container.innerHTML).toBe('<p class="legacy">221°C</p>')
})
