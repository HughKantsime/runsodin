import React, { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Modal from './Modal'

afterEach(cleanup)
function ControlledForm() {
  const [open, setOpen] = useState(false), [value, setValue] = useState('')
  return <><button onClick={() => setOpen(true)}>Open form</button><Modal isOpen={open} title="Controlled form" onClose={() => setOpen(false)}><input aria-label="Customer" value={value} onChange={e => setValue(e.target.value)} /><button>Last control</button></Modal></>
}
it('controlled typing retains all characters and focus while close callback changes', async () => {
  const user = userEvent.setup(); render(<ControlledForm />)
  await user.click(screen.getByRole('button', { name: 'Open form' }))
  const input = screen.getByRole('textbox', { name: 'Customer' }); await user.click(input); await user.type(input, 'Customer name, 123')
  expect(input).toHaveValue('Customer name, 123'); expect(input).toHaveFocus(); expect(screen.getByRole('dialog')).toBeInTheDocument()
  await user.keyboard('{Escape}'); expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Open form' })).toHaveFocus()
})
it('Escape calls the latest committed callback without resetting focused input', () => {
  const first = vi.fn(), latest = vi.fn()
  const view = render(<Modal isOpen title="Latest callback" onClose={first}><input aria-label="Current" /></Modal>)
  const input = screen.getByRole('textbox', { name: 'Current' }); input.focus()
  view.rerender(<Modal isOpen title="Latest callback" onClose={latest}><input aria-label="Current" /></Modal>)
  expect(input).toHaveFocus(); fireEvent.keyDown(document, { key: 'Escape' }); expect(first).not.toHaveBeenCalled(); expect(latest).toHaveBeenCalledTimes(1)
})
it('Tab still wraps between first and last controls after controlled rerenders', async () => {
  const user = userEvent.setup(); render(<ControlledForm />); await user.click(screen.getByRole('button', { name: 'Open form' }))
  const input = screen.getByRole('textbox', { name: 'Customer' }); await user.click(input); await user.type(input, 'abc')
  const controls = screen.getByRole('dialog').querySelectorAll('button,input'); const first = controls[0] as HTMLElement, last = controls[controls.length - 1] as HTMLElement
  last.focus(); await user.tab(); expect(first).toHaveFocus(); await user.tab({ shift: true }); expect(last).toHaveFocus()
})
