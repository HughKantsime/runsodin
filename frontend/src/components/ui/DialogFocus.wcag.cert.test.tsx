// Cert F / cluster D — F12-12.7-04 (security review LOW 6): dialogs exposed as aria-modal
// move focus in on open, trap Tab inside, and restore focus on close; a closed
// DetailDrawer is inert. Real components; behaviour asserted through focus, not attributes.
import { useState } from 'react'
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import DialogSurface from './DialogSurface'
import Modal from './Modal'
import MobileBottomNav from './MobileBottomNav'
import DetailDrawer from '../shared/DetailDrawer'

afterEach(cleanup)

function SurfaceHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open surface</button>
      <button>Behind</button>
      {open && (
        <DialogSurface role="dialog" aria-modal="true" aria-label="Edit" onEscape={() => setOpen(false)}>
          <input aria-label="Name" />
          <button onClick={() => setOpen(false)}>Done</button>
        </DialogSurface>
      )}
    </>
  )
}

it('page-local dialog: focus moves in, Tab is trapped, Escape closes and focus returns to the opener', async () => {
  const user = userEvent.setup()
  render(<SurfaceHarness />)
  const opener = screen.getByRole('button', { name: 'Open surface' })
  await user.click(opener)
  expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus()
  await user.tab()
  expect(screen.getByRole('button', { name: 'Done' })).toHaveFocus()
  await user.tab()                                   // wraps instead of leaving for "Behind"
  expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus()
  await user.tab({ shift: true })
  expect(screen.getByRole('button', { name: 'Done' })).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(opener).toHaveFocus()
})

it('More navigation sheet (MobileBottomNav) takes focus and returns it on close', async () => {
  const user = userEvent.setup()
  render(<MemoryRouter><MobileBottomNav /></MemoryRouter>)
  const more = screen.getByRole('button', { name: /more/i })
  await user.click(more)
  const dialog = screen.getByRole('dialog', { name: 'More navigation' })
  expect(dialog.contains(document.activeElement)).toBe(true)
  await user.click(screen.getByRole('button', { name: 'Close more menu' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(more).toHaveFocus()
})

function DrawerHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Show details</button>
      <DetailDrawer open={open} onClose={() => setOpen(false)} title="Job 7"><a href="#x">Details link</a></DetailDrawer>
    </>
  )
}

it('DetailDrawer: inert while closed; focus in on open, Escape closes and restores focus', async () => {
  const user = userEvent.setup()
  render(<DrawerHarness />)
  const panel = screen.getByText('Job 7').closest('div[class*="fixed inset-y-0"]') as HTMLElement
  expect(panel).toHaveAttribute('inert')
  expect(screen.queryByRole('dialog')).toBeNull()
  const opener = screen.getByRole('button', { name: 'Show details' })
  await user.click(opener)
  expect(panel).not.toHaveAttribute('inert')
  expect(screen.getByRole('dialog', { name: 'Job 7' }).contains(document.activeElement)).toBe(true)
  await user.keyboard('{Escape}')
  expect(panel).toHaveAttribute('inert')
  expect(opener).toHaveFocus()
})

it('shared Modal keeps its focus behaviour after moving to the shared hook', async () => {
  function ModalHarness() {
    const [open, setOpen] = useState(false)
    return (
      <>
        <button onClick={() => setOpen(true)}>Open modal</button>
        <Modal isOpen={open} onClose={() => setOpen(false)} title="Cert modal"><button>Inside</button></Modal>
      </>
    )
  }
  const user = userEvent.setup()
  render(<ModalHarness />)
  const opener = screen.getByRole('button', { name: 'Open modal' })
  await user.click(opener)
  expect(screen.getByRole('dialog', { name: 'Cert modal' }).contains(document.activeElement)).toBe(true)
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(opener).toHaveFocus()
})
