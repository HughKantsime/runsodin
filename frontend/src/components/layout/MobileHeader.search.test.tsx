// Mobile web pass, slice 3: under 640 px the header search is an icon button that expands on tap.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import MobileHeader from './MobileHeader'

vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'ODIN', logo_url: '' }) }))
vi.mock('../shared/GlobalSearch', () => ({ default: ({ id }: { id?: string }) => <input id={id} aria-label="Search field" /> }))
vi.mock('./ThemeToggle', () => ({ default: () => <button>Theme</button> }))
vi.mock('../notifications/AlertBell', () => ({ default: () => <button>Alerts</button> }))

function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('phone: search starts as an icon button and the field is not rendered', () => {
  viewport(true)
  render(<MobileHeader onMenuClick={() => {}} />)
  expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.getByRole('button', { name: 'Open menu' })).toHaveClass('max-sm:min-h-11', 'max-sm:min-w-11')
})

it('phone: tapping the icon expands and focuses the field and hides the title', () => {
  viewport(true)
  render(<MobileHeader onMenuClick={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
  const field = screen.getByRole('textbox')
  expect(field).toHaveFocus()
  expect(screen.queryByRole('button', { name: 'Search' })).toBeNull()
  expect(screen.getByRole('heading', { hidden: true }).parentElement).toHaveClass('hidden')
})

it('phone: Escape collapses and returns focus to the icon', () => {
  viewport(true)
  render(<MobileHeader onMenuClick={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.getByRole('button', { name: 'Search' })).toHaveFocus()
})

it('phone: blur collapses when empty and stays open when there is text', () => {
  viewport(true)
  render(<MobileHeader onMenuClick={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
  const field = screen.getByRole('textbox') as HTMLInputElement
  fireEvent.change(field, { target: { value: 'benchy' } })
  fireEvent.blur(field)
  expect(screen.getByRole('textbox')).toBeInTheDocument()
  fireEvent.change(field, { target: { value: '' } })
  fireEvent.blur(field)
  expect(screen.queryByRole('textbox')).toBeNull()
})

it('640 px and above: the field is always there, no icon button, header classes unchanged', () => {
  viewport(false)
  const { container } = render(<MobileHeader onMenuClick={() => {}} />)
  expect(screen.getByRole('textbox')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Search' })).toBeNull()
  expect(container.querySelector('header')!.className).toBe('mobile-header md:hidden flex items-center justify-between p-4 shrink-0')
  expect(screen.getByRole('textbox').parentElement!.className).toBe('flex items-center gap-2')
  expect(screen.getByRole('heading').parentElement!.className).toBe('flex items-center gap-3')
})
