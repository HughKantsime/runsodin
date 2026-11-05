import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { applyTheme } from '../../hooks/themePreferences'
import Projects from './Projects'

vi.mock('../../api', () => ({ projects: { list: async () => [] } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(() => { cleanup(); localStorage.clear(); applyTheme('dark', 'classic') })
it.each(['classic', 'foundry'] as const)('%s names all ten swatches and exposes only the selected colour as pressed', family => {
  localStorage.setItem('odin-theme-family', family)
  localStorage.setItem('odin-theme', 'dark')
  applyTheme('dark', family)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Projects /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'New' }))
  const dialog = within(screen.getByRole('dialog', { name: 'New Project' }))
  expect(dialog.getAllByRole('button', { name: /^Colour: #[0-9a-f]{6}$/ })).toHaveLength(10)
  expect(dialog.getByRole('button', { name: 'Colour: #6366f1' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(dialog.getByRole('button', { name: 'Colour: #3b82f6' }))
  expect(dialog.getByRole('button', { name: 'Colour: #6366f1' })).toHaveAttribute('aria-pressed', 'false')
  expect(dialog.getByRole('button', { name: 'Colour: #3b82f6' })).toHaveAttribute('aria-pressed', 'true')
})
