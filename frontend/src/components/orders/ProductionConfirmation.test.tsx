import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ProductionConfirmation } from './OrderModals'
afterEach(cleanup)
const item = { id: 19, quantity: 3, fulfilled_quantity: 1 }
it('submits explicit quantity including a correction to zero', async () => {
  const confirm = vi.fn().mockResolvedValue(undefined)
  render(<ProductionConfirmation item={item} onConfirm={confirm} />)
  fireEvent.change(screen.getByLabelText('Produced quantity'), { target: { value: '0' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm production' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(19, 0))
})
it('disables empty, fractional and excessive quantities', () => {
  render(<ProductionConfirmation item={item} onConfirm={vi.fn()} />)
  for (const value of ['', '1.5', '4', '-1']) {
    fireEvent.change(screen.getByLabelText('Produced quantity'), { target: { value } })
    expect(screen.getByRole('button', { name: 'Confirm production' })).toBeDisabled()
  }
})
it('prevents duplicate submissions while saving and permits retry after rejection', async () => {
  let reject!: (reason: Error) => void
  const confirm = vi.fn(() => new Promise<void>((resolve, fail) => { reject = fail }))
  render(<ProductionConfirmation item={item} onConfirm={confirm} />)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm production' }))
  expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()
  expect(screen.getByLabelText('Produced quantity')).toBeDisabled()
  reject(new Error('Synthetic rejection'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm production' })).toBeEnabled())
  expect(confirm).toHaveBeenCalledTimes(1)
})
