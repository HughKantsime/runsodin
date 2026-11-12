// Mobile web pass, slice 4: Projects pop-ups get phone-sheet classes (max-sm only); 640 px and wider keep today's classes.
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import { CreateProjectModal, ProjectDetail } from './Projects'

vi.mock('../../api', () => ({ projects: { get: () => new Promise(() => {}), update: vi.fn(), exportZip: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(cleanup)
const desktop = (cls: string) => cls.split(/\s+/).filter(t => t && !t.startsWith('max-sm:')).join(' ')
const CLOSE = ['max-sm:min-h-11', 'max-sm:min-w-11']

test('CreateProjectModal: desktop classes unchanged, phone sheet tokens, footer marker', () => {
  render(<CreateProjectModal onClose={() => {}} onCreate={() => {}} />)
  const panel = screen.getByRole('dialog')
  expect(desktop(panel.parentElement!.className)).toBe('fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4')
  expect(panel.parentElement).toHaveClass('max-sm:p-0')
  expect(desktop(panel.className)).toBe('bg-[var(--brand-card-bg)] rounded-xl border border-[var(--brand-card-border)] w-full max-w-md p-4 sm:p-6')
  expect(panel).toHaveClass('max-sm:h-full', 'max-sm:max-w-none', 'max-sm:rounded-none', 'max-sm:overflow-y-auto')
  const close = screen.getByLabelText('Close new project dialog')
  expect(desktop(close.className)).toBe('text-[var(--brand-muted)] hover:text-white')
  expect(close).toHaveClass(...CLOSE)
  const footer = screen.getByText('Create').parentElement!
  expect(footer).toHaveAttribute('data-modal-footer')
  expect(desktop(footer.className)).toBe('flex justify-end gap-2 pt-2')
  expect(footer).toHaveClass('max-sm:sticky', 'max-sm:bottom-0')
})

test('ProjectDetail: desktop classes unchanged, phone sheet tokens, no invented footer', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { container } = render(<QueryClientProvider client={client}><ProjectDetail project={{ id: 1, name: 'Proj', color: '#6366f1' }} onClose={() => {}} onDelete={() => {}} /></QueryClientProvider>)
  const panel = screen.getByRole('dialog')
  expect(desktop(panel.parentElement!.className)).toBe('fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4')
  expect(desktop(panel.className)).toBe('bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded-xl border border-[var(--brand-card-border)] w-full max-w-lg p-4 sm:p-6 max-h-[90vh] overflow-y-auto')
  expect(panel).toHaveClass('max-sm:h-full', 'max-sm:max-h-none', 'max-sm:max-w-none', 'max-sm:rounded-none')
  const close = screen.getByLabelText('Close project details')
  expect(desktop(close.className)).toBe('text-[var(--brand-muted)] hover:text-white')
  expect(close).toHaveClass(...CLOSE)
  expect(container.querySelector('[data-modal-footer]')).toBeNull()
})
