// Mobile web pass, slice 3: Modal is a full-screen sheet under 640 px and unchanged above.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import Modal from './Modal'

function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const panel = () => screen.getByRole('dialog').firstElementChild as HTMLElement

test('phone: fills the viewport, body scrolls, footer marker is sticky, close is 44 px, inputs are 16 px', () => {
  viewport(true)
  render(<Modal isOpen onClose={() => {}} title="T"><div data-modal-footer>f</div></Modal>)
  expect(screen.getByRole('dialog')).toHaveClass('items-stretch')
  expect(screen.getByRole('dialog')).not.toHaveClass('items-end')
  expect(panel()).toHaveClass('h-full', 'max-w-none', 'flex', 'flex-col', 'overflow-hidden')
  expect(panel()).not.toHaveClass('max-h-[90vh]')
  const close = screen.getByLabelText('Close')
  expect(close).toHaveClass('min-h-11', 'min-w-11')
  const body = screen.getByText('f').parentElement!
  expect(body).toHaveClass('flex-1', 'min-h-0', 'overflow-y-auto', '[&_input]:text-base', '[&_select]:text-base', '[&_textarea]:text-base')
  expect(body.className).toContain('[&_[data-modal-footer]]:sticky')
})

test('phone: a modal without a title still gets a close button', () => {
  viewport(true)
  render(<Modal isOpen onClose={() => {}}>x</Modal>)
  expect(screen.getByLabelText('Close')).toBeInTheDocument()
})

test('phone: alert dialogs keep their compact layout', () => {
  viewport(true)
  render(<Modal isOpen onClose={() => {}} title="Sure?" size="sm" alert>x</Modal>)
  const alertPanel = screen.getByRole('alertdialog').firstElementChild as HTMLElement
  expect(alertPanel).not.toHaveClass('h-full')
  expect(alertPanel).toHaveClass('max-h-[90vh]')
})

test('640 px and above: classes and DOM order are unchanged', () => {
  viewport(false)
  render(<Modal isOpen onClose={() => {}} title="T">body</Modal>)
  expect(screen.getByRole('dialog').className).toBe('fixed inset-0 bg-black/60 flex justify-center z-50 p-0 sm:p-4 items-end sm:items-center')
  expect(panel().className.trim()).toBe('bg-[var(--brand-card-bg)] w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-t-xl sm:rounded-lg')
  expect(panel().children).toHaveLength(2)
  expect(panel().children[0].className).toBe('flex items-center justify-between p-4 sm:p-6 pb-0')
  expect(panel().children[1].className).toBe('p-4 sm:p-6')
  expect(screen.getByLabelText('Close').className).toBe('text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)]')
})

test('no matchMedia (jsdom default) renders the desktop layout', () => {
  render(<Modal isOpen onClose={() => {}} title="T">body</Modal>)
  expect(panel()).toHaveClass('max-h-[90vh]')
})
