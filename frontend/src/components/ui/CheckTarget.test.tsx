import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { applyTheme } from '../../hooks/themePreferences'
import CheckTarget from './CheckTarget'

afterEach(() => { cleanup(); localStorage.clear(); applyTheme('dark', 'classic') })
it.each(['classic', 'foundry'] as const)('%s keeps native checked semantics and the change handler', family => {
  localStorage.setItem('odin-theme-family', family)
  localStorage.setItem('odin-theme', 'dark')
  applyTheme('dark', family)
  const change = vi.fn()
  const { container } = render(<CheckTarget><input type="checkbox" aria-label="Select job" onChange={change} /></CheckTarget>)
  const checkbox = screen.getByRole('checkbox', { name: 'Select job' })
  if (family === 'foundry') {
    expect(checkbox.closest('label')).toHaveClass('f-check-target')
    fireEvent.click(checkbox.closest('label')!)
  } else {
    expect(container.firstElementChild).toBe(checkbox)
    fireEvent.click(checkbox)
  }
  expect(change).toHaveBeenCalledTimes(1)
  expect(checkbox).toBeChecked()
})
it.each(['light', 'high-contrast'] as const)('%s retains the Classic input without a wrapper', mode => {
  localStorage.setItem('odin-theme-family', 'foundry')
  localStorage.setItem('odin-theme', mode)
  applyTheme(mode, 'foundry')
  const { container } = render(<CheckTarget><input type="checkbox" aria-label="Select job" /></CheckTarget>)
  expect(container.firstElementChild?.tagName).toBe('INPUT')
})
