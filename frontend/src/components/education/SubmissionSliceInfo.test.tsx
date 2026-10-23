import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SubmissionSliceInfo from './SubmissionSliceInfo'
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: {
  selected_plate_index: 0, plates: [{ plate: '1', estimated_seconds: '120', filaments: [{ source_slot: '1', material: 'PLA', color: '#FFFFFF' }] }],
  slots: [{ slot_number: 1, filament_type: 'PETG', color_hex: '123ABC', loaded_at: '2026-10-22 12:00:00' }], freshness: 'Last recorded slot state',
} }) }))
afterEach(cleanup)
it('shows sliced PLA beside loaded PETG, freshness and a visible warning', () => {
  render(<SubmissionSliceInfo id={1} revision={1} printerId={9} />)
  expect(screen.getByText(/Sliced PLA/)).toHaveTextContent('source slot 1')
  expect(screen.getByText(/Slot 1: PETG/)).toHaveTextContent('observed 2026-10-22')
  expect(screen.getByText('Last recorded slot state')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('Filament mismatch')
})
