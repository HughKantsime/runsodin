import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { EditJobModal } from './JobModals'
import JobRow from './JobRow'
afterEach(cleanup)
const job = { id: 21, item_name: 'Synthetic historical job', status: 'pending', quantity: null, hold: null, is_locked: null, priority: 3, colors_list: [] }
it('preserves unknown quantity during unrelated edits', () => {
  const submit = vi.fn()
  render(<EditJobModal isOpen job={job} onClose={vi.fn()} onSubmit={submit} />)
  expect(screen.getByLabelText('Quantity')).toHaveValue(null)
  fireEvent.click(screen.getByRole('button', { name: /save/i }))
  expect(submit).toHaveBeenCalledTimes(1)
  expect(submit.mock.calls[0][1]).not.toHaveProperty('quantity')
})
it('submits an explicitly selected quantity', () => {
  const submit = vi.fn()
  render(<EditJobModal isOpen job={job} onClose={vi.fn()} onSubmit={submit} />)
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '2' } })
  fireEvent.click(screen.getByRole('button', { name: /save/i }))
  expect(submit.mock.calls[0][1].quantity).toBe(2)
})
it('shows unknown operational fields honestly in the row', () => {
  render(<table><tbody><JobRow job={job} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} /></tbody></table>)
  expect(screen.getByText(/Quantity unknown.*Hold state unknown.*Lock state unknown/)).toBeInTheDocument()
})
