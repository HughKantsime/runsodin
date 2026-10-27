import { expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import JobTimeline from './JobTimeline'
vi.mock('@tanstack/react-query', () => ({useQuery: () => ({data: {events: [{id: '1', kind: 'nozzle_unverified', occurred_at: '2026-10-27T12:00:00Z', detail: {geometry_unverified: true}}, {id: '2', kind: 'ams_mapping_changed', occurred_at: '2026-10-27T12:00:01Z', detail: {ams_mapping: [0, 3]}}]}})}))
it('explains missing per-object geometry assignments', () => {
  render(<JobTimeline jobId={53} />)
  expect(screen.getByText(/Per-nozzle reach was not verified: object assignments were missing/)).toBeInTheDocument()
  expect(screen.queryByText(/file or printer data was missing/)).not.toBeInTheDocument()
})

it('preserves AMS mapping history alongside nozzle diagnostics', () => {
  render(<JobTimeline jobId={53} />)
  expect(screen.getByText(/Filament 1 → Slot 1, Filament 2 → Slot 4/)).toBeInTheDocument()
})
