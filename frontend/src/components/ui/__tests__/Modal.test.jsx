import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Modal from '../Modal'

describe('Modal', () => {
  it('renders nothing when isOpen is false', () => {
    const { container } = render(
      <Modal isOpen={false} onClose={() => {}}>
        Content
      </Modal>
    )
    expect(container.innerHTML).toBe('')
  })

  it('renders children when isOpen is true', () => {
    render(
      <Modal isOpen={true} onClose={() => {}}>
        <p>Modal content</p>
      </Modal>
    )
    expect(screen.getByText('Modal content')).toBeInTheDocument()
  })

  it('displays the title when provided', () => {
    render(
      <Modal isOpen={true} onClose={() => {}} title="Test Title">
        Content
      </Modal>
    )
    expect(screen.getByText('Test Title')).toBeInTheDocument()
  })

  it('does not render title element when title is not provided', () => {
    render(
      <Modal isOpen={true} onClose={() => {}}>
        Content
      </Modal>
    )
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
  })

  it('calls onClose when Escape key is pressed', async () => {
    const user = userEvent.setup()
    const handleClose = vi.fn()
    render(
      <Modal isOpen={true} onClose={handleClose} title="Escape Test">
        Content
      </Modal>
    )

    await user.keyboard('{Escape}')
    expect(handleClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when backdrop is clicked', async () => {
    const user = userEvent.setup()
    const handleClose = vi.fn()
    render(
      <Modal isOpen={true} onClose={handleClose} title="Backdrop Test">
        Content
      </Modal>
    )

    // Click the backdrop (the outer dialog overlay)
    const backdrop = screen.getByRole('dialog')
    await user.click(backdrop)
    expect(handleClose).toHaveBeenCalled()
  })

  it('does not call onClose when modal content is clicked', async () => {
    const user = userEvent.setup()
    const handleClose = vi.fn()
    render(
      <Modal isOpen={true} onClose={handleClose} title="Content Click Test">
        <button>Inner button</button>
      </Modal>
    )

    await user.click(screen.getByText('Inner button'))
    expect(handleClose).not.toHaveBeenCalled()
  })

  it('has correct aria role for dialog', () => {
    render(
      <Modal isOpen={true} onClose={() => {}} title="ARIA Test">
        Content
      </Modal>
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('has correct aria role for alert dialog', () => {
    render(
      <Modal isOpen={true} onClose={() => {}} title="Alert Test" alert>
        Content
      </Modal>
    )
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('renders a close button with aria-label', () => {
    render(
      <Modal isOpen={true} onClose={() => {}} title="Close Btn Test">
        Content
      </Modal>
    )
    expect(screen.getByLabelText('Close')).toBeInTheDocument()
  })

  it('calls onClose when the X close button is clicked', async () => {
    const user = userEvent.setup()
    const handleClose = vi.fn()
    render(
      <Modal isOpen={true} onClose={handleClose} title="X Button Test">
        Content
      </Modal>
    )

    await user.click(screen.getByLabelText('Close'))
    expect(handleClose).toHaveBeenCalledTimes(1)
  })
})
