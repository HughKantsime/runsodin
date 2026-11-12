// Mobile web pass, slice 4: ModelRevisionPanel gets phone-sheet classes (max-sm only); 640 px and wider keep today's classes.
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import ModelRevisionPanel from './ModelRevisionPanel'

vi.mock('../../api', () => ({ modelRevisions: { list: vi.fn().mockResolvedValue([]), create: vi.fn(), revert: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(cleanup)
const desktop = (cls: string) => cls.split(/\s+/).filter(t => t && !t.startsWith('max-sm:')).join(' ')

test('desktop classes unchanged, phone sheet tokens, 44 px close', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ModelRevisionPanel modelId={1} modelName="M" onClose={() => {}} /></QueryClientProvider>)
  const overlay = screen.getByRole('dialog')
  expect(overlay.className).toBe('fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4')
  const panel = overlay.firstElementChild as HTMLElement
  expect(desktop(panel.className)).toBe('bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded w-full max-w-lg p-4 sm:p-6 border border-[var(--brand-card-border)] max-h-[90vh] overflow-y-auto')
  expect(panel).toHaveClass('max-sm:h-full', 'max-sm:max-h-none', 'max-sm:max-w-none', 'max-sm:rounded-none')
  const close = screen.getByLabelText('Close revisions')
  expect(desktop(close.className)).toBe('text-[var(--brand-muted)] hover:text-[var(--brand-text-secondary)]')
  expect(close).toHaveClass('max-sm:min-h-11', 'max-sm:min-w-11')
})
