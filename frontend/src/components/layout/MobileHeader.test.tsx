import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import MobileHeader from './MobileHeader'
import { applyTheme } from '../../hooks/themePreferences'
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'ODIN', logo_url: '/logo.png' }) }))
vi.mock('../shared/GlobalSearch', () => ({ default: () => <input aria-label="Search" /> }))
vi.mock('./ThemeToggle', () => ({ default: () => <button>Theme</button> }))
vi.mock('../notifications/AlertBell', () => ({ default: () => <button>Alerts</button> }))
afterEach(() => { cleanup(); applyTheme('dark', 'classic') })
it('Classic keeps the pre-Foundry header spacing and search container at phone width', () => {
  applyTheme('dark', 'classic')
  const { container } = render(<MobileHeader onMenuClick={() => {}} />)
  expect(container.querySelector('header')!.className).toBe('mobile-header md:hidden flex items-center justify-between p-4 shrink-0')
  expect(screen.getByRole('heading').className).toBe('text-sm font-display font-bold')
  expect(screen.getByRole('heading').parentElement!.className).toBe('flex items-center gap-3')
  expect(screen.getByRole('img').className).toBe('h-6 object-contain')
  expect(screen.getByRole('textbox').parentElement!.className).toBe('flex items-center gap-2')
})
it('Foundry retains the responsive search and logo constraints', () => {
  applyTheme('dark', 'foundry')
  render(<MobileHeader onMenuClick={() => {}} />)
  expect(screen.getByRole('textbox').parentElement).toHaveClass('mobile-header-actions', 'flex-1', 'min-w-0')
  expect(screen.getByRole('heading')).toHaveClass('truncate')
})
