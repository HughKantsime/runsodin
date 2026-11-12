// Mobile web pass, slice 4: Admin pop-ups get phone-sheet classes (max-sm only); 640 px and wider keep today's classes.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import { UserModal, ImportUsersModal } from './Admin'

afterEach(cleanup)
const desktop = (cls: string) => cls.split(/\s+/).filter(t => t && !t.startsWith('max-sm:')).join(' ')
const SHEET = ['max-sm:h-full', 'max-sm:max-h-none', 'max-sm:max-w-none', 'max-sm:rounded-none']
const CLOSE = ['max-sm:min-h-11', 'max-sm:min-w-11']
const FOOTER = ['max-sm:sticky', 'max-sm:bottom-0', 'max-sm:bg-[var(--brand-card-bg)]']

test('UserModal: desktop classes unchanged, phone sheet tokens, footer marker', () => {
  render(<UserModal user={null} hasGroups={false} onClose={() => {}} onSave={() => {}} />)
  const panel = screen.getByRole('dialog')
  expect(panel.parentElement!.className).toBe('fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4')
  expect(desktop(panel.className)).toBe('bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded border border-[var(--brand-card-border)] w-full max-w-md p-4 sm:p-6 max-h-[90vh] overflow-y-auto')
  expect(panel).toHaveClass(...SHEET)
  const close = screen.getByLabelText('Close new user dialog')
  expect(desktop(close.className)).toBe('text-[var(--brand-text-muted)] hover:text-white')
  expect(close).toHaveClass(...CLOSE)
  const footer = screen.getByText('Create User').parentElement!
  expect(footer).toHaveAttribute('data-modal-footer')
  expect(desktop(footer.className)).toBe('flex gap-3 pt-4')
  expect(footer).toHaveClass(...FOOTER)
})

test('ImportUsersModal: desktop classes unchanged, phone sheet tokens, footer marker', () => {
  render(<ImportUsersModal onClose={() => {}} onImported={() => {}} />)
  const panel = screen.getByRole('dialog')
  expect(desktop(panel.className)).toBe('bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded border border-[var(--brand-card-border)] w-full max-w-lg p-4 sm:p-6 max-h-[90vh] overflow-y-auto')
  expect(panel).toHaveClass(...SHEET)
  const close = screen.getByLabelText('Close import users dialog')
  expect(desktop(close.className)).toBe('text-[var(--brand-text-muted)] hover:text-white')
  expect(close).toHaveClass(...CLOSE)
  const footer = screen.getByText('Cancel').parentElement!
  expect(footer).toHaveAttribute('data-modal-footer')
  expect(desktop(footer.className)).toBe('flex gap-3')
  expect(footer).toHaveClass(...FOOTER)
})
