import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { BrandingProvider, useBrandingUpdate } from './BrandingContext'
import OdinMark from './components/layout/OdinMark'

const stock = { app_name: 'O.D.I.N.', logo_url: null, favicon_url: null }
const neutralIcon = '/favicon-neutral.svg'
const response = (data: unknown, ok = true) => ({ ok, json: async () => data })
const icons = () => [...document.querySelectorAll<HTMLLinkElement>('link[rel~="icon"]')]

function loadPageIcons() {
  const html = readFileSync(resolve(__dirname, '../index.html'), 'utf8')
  const page = new DOMParser().parseFromString(html, 'text/html')
  page.querySelectorAll('link[rel~="icon"]').forEach(link => document.head.appendChild(link.cloneNode(true)))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  document.title = ''
  icons().forEach(link => link.remove())
})

test.each(['/api/branding', '/api/auth/me', '/api/license', '/api/orgs/7/settings'])(
  'withholds the ODIN mark while %s is pending on a white-labelled install', async pendingUrl => {
    loadPageIcons()
    let release!: (value: unknown) => void
    const delayed = new Promise(resolve => { release = resolve })
    const replies: Record<string, ReturnType<typeof response>> = {
      '/api/branding': response(pendingUrl === '/api/branding' ? { ...stock, app_name: 'Custom farm' } : stock),
      '/api/auth/me': response({ group_id: 7 }),
      '/api/license': response({ features: ['branding'] }),
      '/api/orgs/7/settings': response({ branding_app_name: 'Custom farm' }),
    }
    const fetcher = vi.fn(async (url: string) => url === pendingUrl ? delayed : replies[url])
    vi.stubGlobal('fetch', fetcher)
    render(<BrandingProvider><OdinMark /></BrandingProvider>)
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === pendingUrl)).toBe(true))
    expect(screen.queryByTestId('odin-mark')).not.toBeInTheDocument()
    for (const link of icons()) expect(link).toHaveAttribute('href', neutralIcon)
    await act(async () => { release(replies[pendingUrl]); await delayed })
    expect(screen.queryByTestId('odin-mark')).not.toBeInTheDocument()
    expect(document.title).toBe('Custom farm Print Farm Management')
    for (const link of icons()) expect(link).toHaveAttribute('href', neutralIcon)
  },
)

test('shows the stock mark only after branding resolves', async () => {
  let release!: (value: unknown) => void
  const delayed = new Promise(resolve => { release = resolve })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/branding' ? delayed : response({}, false)))
  render(<BrandingProvider><OdinMark /></BrandingProvider>)
  expect(screen.queryByTestId('odin-mark')).not.toBeInTheDocument()
  await act(async () => { release(response(stock)); await delayed })
  expect(screen.getByTestId('odin-mark')).toBeInTheDocument()
})

test('a failed branding request completes the existing stock fallback', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('Unavailable') }))
  render(<BrandingProvider><OdinMark /></BrandingProvider>)
  expect(await screen.findByTestId('odin-mark')).toBeInTheDocument()
})

test('the initial HTML has neutral tab icons until branding resolves', () => {
  loadPageIcons()
  expect(icons()).toHaveLength(2)
  for (const link of icons()) expect(link).toHaveAttribute('href', neutralIcon)
})

test.each([
  ['custom name', { ...stock, app_name: 'Custom farm' }, neutralIcon],
  ['custom logo', { ...stock, logo_url: '/custom-logo.png' }, neutralIcon],
  ['custom favicon', { ...stock, app_name: 'Custom farm', favicon_url: '/custom-icon.png' }, '/custom-icon.png'],
  ['stock branding', stock, '/favicon.svg'],
])('selects both browser icon candidates for %s', async (_label, brand, expected) => {
  loadPageIcons()
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/branding' ? response(brand) : response({}, false)))
  render(<BrandingProvider><OdinMark /></BrandingProvider>)
  await waitFor(() => expect(document.title).toBe(`${brand.app_name} Print Farm Management`))
  expect(icons()).toHaveLength(2)
  for (const link of icons()) {
    const fallback = expected === '/favicon.svg' && link.rel === 'alternate icon'
    expect(link).toHaveAttribute('href', fallback ? '/favicon-32.png' : expected)
  }
})

function UpdateButtons() {
  const update = useBrandingUpdate()
  return <>
    <button onClick={() => update({ favicon_url: null })}>Remove favicon</button>
    <button onClick={() => update(stock)}>Restore stock</button>
    <button onClick={() => update({ logo_url: '/custom-logo.png' })}>Set logo</button>
  </>
}

test('favicon removal and confirmed identity changes update all icon candidates', async () => {
  loadPageIcons()
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/branding'
    ? response({ ...stock, app_name: 'Custom farm', favicon_url: '/custom-icon.png' }) : response({}, false)))
  render(<BrandingProvider><UpdateButtons /></BrandingProvider>)
  await waitFor(() => expect(icons()[0]).toHaveAttribute('href', '/custom-icon.png'))
  fireEvent.click(screen.getByText('Remove favicon'))
  for (const link of icons()) expect(link).toHaveAttribute('href', neutralIcon)
  fireEvent.click(screen.getByText('Restore stock'))
  expect(icons().map(link => link.getAttribute('href'))).toEqual(['/favicon.svg', '/favicon-32.png'])
  fireEvent.click(screen.getByText('Set logo'))
  for (const link of icons()) expect(link).toHaveAttribute('href', neutralIcon)
})
