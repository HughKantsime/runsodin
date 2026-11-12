// Mobile web pass, slice 4: GroupModal gets phone-sheet classes (max-sm only); 640 px and wider keep today's classes.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { GroupModal } from './GroupManager'

vi.mock('../../api', () => ({ groups: {}, users: {} }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => true }) }))
afterEach(cleanup)
const desktop = (cls: string) => cls.split(/\s+/).filter(t => t && !t.startsWith('max-sm:')).join(' ')

test('GroupModal: desktop classes unchanged, phone sheet tokens, footer marker', () => {
  render(<GroupModal group={null} operatorAdmins={[]} onClose={() => {}} onSave={() => {}} />)
  const overlay = screen.getByRole('dialog')
  expect(overlay.className).toBe('fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4')
  const panel = overlay.firstElementChild as HTMLElement
  expect(desktop(panel.className)).toBe('bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded border border-[var(--brand-card-border)] w-full max-w-md p-4 sm:p-6 max-h-[90vh] overflow-y-auto')
  expect(panel).toHaveClass('max-sm:h-full', 'max-sm:max-h-none', 'max-sm:max-w-none', 'max-sm:rounded-none')
  const close = screen.getByLabelText('Close')
  expect(desktop(close.className)).toBe('text-[var(--brand-text-muted)] hover:text-white')
  expect(close).toHaveClass('max-sm:min-h-11', 'max-sm:min-w-11')
  const footer = screen.getByText('Create Group').parentElement!
  expect(footer).toHaveAttribute('data-modal-footer')
  expect(desktop(footer.className)).toBe('flex gap-3 pt-4')
  expect(footer).toHaveClass('max-sm:sticky', 'max-sm:bottom-0')
})
