import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import AlertTitle, { LONG_ALERT_TITLE } from './AlertTitle'

describe('AlertTitle', () => {
  it('renders a short title as plain text, unchanged', () => {
    const { container } = render(<AlertTitle title="Xena paused" />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(container.firstElementChild?.className).toBe('text-sm font-semibold')
    expect(container.textContent).toBe('Xena paused')
  })

  it('clamps a long title to two lines and expands it in place when pressed', () => {
    const title = 'Hubble: ' + 'Pikachu Magnet 12-up 5-colour '.repeat(4) + 'paused'
    expect(title.length).toBeGreaterThan(LONG_ALERT_TITLE)
    render(<AlertTitle title={title} />)
    const button = screen.getByRole('button')
    expect(button.textContent).toBe(title)
    const text = button.querySelector('[data-alert-title]') as HTMLElement
    expect(text.className).toContain('line-clamp-2')
    expect(text.className.split(' ')).not.toContain('block')   // never two display rules on the clamped box
    expect(button.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(button)
    expect(text.className).not.toContain('line-clamp-2')
    expect(button.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(button)
    expect(text.className).toContain('line-clamp-2')
  })
})
