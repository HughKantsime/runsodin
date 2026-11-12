import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import StatusBadge from './StatusBadge'

vi.mock('../../hooks/themePreferences', () => ({ useFoundry: () => true }))
afterEach(cleanup)

describe('Foundry StatusBadge for jobs', () => {
  it.each([
    ['completed', 'Completed'],
    ['failed', 'Failed'],
    ['cancelled', 'Cancelled'],
  ])('labels a %s job %s', (status, label) => {
    render(<StatusBadge status={status} kind="job" />)
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  })

  it('keeps the other job statuses on the shared mapping', () => {
    render(<StatusBadge status="scheduled" kind="job" />)
    expect(screen.getByText('Queued')).toBeInTheDocument()
  })

  it('still reads a finished printer state as Ready when no kind is given', () => {
    render(<StatusBadge status="completed" />)
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })
})
