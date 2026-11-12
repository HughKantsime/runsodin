import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import NeedsYouBanner from './NeedsYouBanner'

vi.mock('../../permissions', () => ({ canAccessPage: () => false, canDo: () => false, getCurrentUser: () => null }))
afterEach(cleanup)

const now = Date.parse('2026-11-12T12:00:00Z')
const printer = { id: 1, name: 'Hubble', gcode_state: 'PAUSE', last_seen: '2026-11-12T11:59:00Z', is_active: true } as any
const alertAgo = (minutes: number) => ({
  id: 1, printer_id: 1, alert_type: 'print_paused', message: 'Spaghetti',
  created_at: new Date(now - minutes * 60000).toISOString(),
}) as any

function show(minutes: number) {
  return render(<MemoryRouter><NeedsYouBanner printers={[printer]} alerts={[alertAgo(minutes)]} now={now} /></MemoryRouter>)
}

describe('Floor banner age', () => {
  it('uses minutes under an hour', () => { show(6); expect(screen.getByText(/Reported 6 minutes ago/)).toBeInTheDocument() })
  it('uses hours under 48 hours', () => { show(491); expect(screen.getByText(/Reported 8 hours ago/)).toBeInTheDocument() })
  it('uses days after that, never raw minutes', () => {
    show(5120)
    expect(screen.getByText(/Reported 3 days ago/)).toBeInTheDocument()
    expect(screen.queryByText(/5120/)).not.toBeInTheDocument()
  })
})
