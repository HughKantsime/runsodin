// Cert-E integ F15: real BrandingProvider / Branding admin page fed by the producer wire
// fixture captured from the real backend after an admin save + logo upload
// (tests/test_contracts/test_cert_E_integ_branding.py).
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BrandingProvider, useBranding } from './BrandingContext'
import Branding from './pages/admin/Branding'
import wire from './test-fixtures/cert-E-integ-branding.json'

const response = (data: any, ok = true) => ({ ok, json: async () => data })

afterEach(() => {
  cleanup(); vi.unstubAllGlobals(); document.title = ''
  document.documentElement.removeAttribute('style'); document.documentElement.className = ''
})

function Header() {
  // Same branding fields the shipped Sidebar/MobileHeader render.
  const b = useBranding()
  return <header>{b.logo_url && <img src={b.logo_url} alt={b.app_name} />}<h1>{b.app_name}</h1><p>{b.app_subtitle}</p></header>
}

describe('CertEInteg branding', () => {
  it('applies saved branding through --brand-* CSS custom properties, title and identity', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: any) => url === '/api/branding' ? response(wire) : response({}, false)))
    render(<BrandingProvider><Header /></BrandingProvider>)
    await screen.findByRole('heading', { name: wire.app_name })
    expect(screen.getByText(wire.app_subtitle)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: wire.app_name })).toHaveAttribute('src', wire.logo_url)
    const style = document.documentElement.style
    await waitFor(() => expect(style.getPropertyValue('--brand-primary')).toBe(wire.primary_color))
    expect(style.getPropertyValue('--brand-accent')).toBe(wire.accent_color)
    expect(style.getPropertyValue('--brand-font-display')).toBe(wire.font_display)
    expect(style.getPropertyValue('--brand-font-body')).toBe(wire.font_body)
    expect(style.getPropertyValue('--brand-font-mono')).toBe(wire.font_mono)
    expect(style.getPropertyValue('--brand-sidebar-bg')).toBe(wire.sidebar_bg)
    expect(document.title).toBe(`${wire.app_name} ${wire.app_subtitle}`)
  })

  it('Typography offers 28 display + 24 body + 12 mono = 64 entries (46 distinct names) and reflects the saved stacks', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(wire)))
    render(<Branding />)
    await screen.findByDisplayValue(wire.app_name)
    fireEvent.click(screen.getByRole('button', { name: /Typography/ }))
    const selects = await waitFor(() => {
      const found = Array.from(document.querySelectorAll('select'))
      expect(found.length).toBe(3)
      return found
    })
    const counts = selects.map(s => s.options.length)
    expect(counts).toEqual([28, 24, 12])
    expect(counts.reduce((a, b) => a + b, 0)).toBe(64)
    const names = new Set(selects.flatMap(s => Array.from(s.options).map(o => o.textContent)))
    expect(names.size).toBe(46)
    expect(selects.map(s => s.value)).toEqual([wire.font_display, wire.font_body, wire.font_mono])
    expect(selects.map(s => s.selectedOptions[0].textContent)).toEqual(['Space Grotesk', 'Inter', 'JetBrains Mono'])
  })

  it('admin edits name, subtitle, primary/accent and uploads a logo; saved response re-themes the app', async () => {
    const before = { ...wire, app_name: 'Synthetic before', app_subtitle: 'Synthetic old', primary_color: '#111111', accent_color: '#222222', logo_url: null }
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: any, options: any = {}) => {
      calls.push([url, options.method || 'GET', options.body])
      if (url === '/api/branding' && options.method === 'PUT') return response(wire)
      if (url === '/api/branding/logo' && options.method === 'POST') return response({ logo_url: wire.logo_url })
      if (url === '/api/branding') return response(before)
      return response({}, false)
    }))
    render(<BrandingProvider><Branding /></BrandingProvider>)
    fireEvent.change(await screen.findByDisplayValue('Synthetic before'), { target: { value: wire.app_name } })
    fireEvent.change(screen.getByDisplayValue('Synthetic old'), { target: { value: wire.app_subtitle } })
    fireEvent.change(screen.getAllByDisplayValue('#111111').find(e => e.getAttribute('type') === 'text')!, { target: { value: wire.primary_color } })
    fireEvent.change(screen.getAllByDisplayValue('#222222').find(e => e.getAttribute('type') === 'text')!, { target: { value: wire.accent_color } })
    const fileInputs = document.querySelectorAll<HTMLInputElement>('input[type="file"]')
    fireEvent.change(fileInputs[0], { target: { files: [new File(['synthetic'], 'logo.png', { type: 'image/png' })] } })
    await waitFor(() => expect(calls.some(c => c[0] === '/api/branding/logo' && c[1] === 'POST')).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: /Save Changes/ }))
    await waitFor(() => expect(calls.some(c => c[1] === 'PUT')).toBe(true))
    const body = JSON.parse(calls.find(c => c[1] === 'PUT')[2])
    expect(body).toMatchObject({ app_name: wire.app_name, app_subtitle: wire.app_subtitle, primary_color: wire.primary_color, accent_color: wire.accent_color })
    await waitFor(() => expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe(wire.primary_color))
    expect(document.documentElement.style.getPropertyValue('--brand-accent')).toBe(wire.accent_color)
    await waitFor(() => expect(document.title).toBe(`${wire.app_name} ${wire.app_subtitle}`))
    expect(screen.getAllByAltText('Custom logo')[0]).toHaveAttribute('src', wire.logo_url)
  })
})
