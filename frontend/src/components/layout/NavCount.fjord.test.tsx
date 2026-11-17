import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { NavCount } from './Sidebar'
afterEach(cleanup)
it('lets Fjord supply readable alert count text while retaining the Classic fallback', () => {
  render(<NavCount count={3} label="Unread alerts" tone="hot" />)
  expect(screen.getByRole('status')).toHaveStyle({ color: 'var(--fjord-on-failed, #fff)', backgroundColor: 'var(--status-failed, #dc2626)' })
})
