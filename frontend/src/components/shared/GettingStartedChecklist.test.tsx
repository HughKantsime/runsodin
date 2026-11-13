import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import GettingStartedChecklist from './GettingStartedChecklist'
import classicDashboard from '../../pages/dashboard/Dashboard.tsx?raw'
import floor from '../foundry/Floor.tsx?raw'
import app from '../../App.tsx?raw'

const h = vi.hoisted(() => ({
  printers: vi.fn(), jobs: vi.fn(), files: vi.fn(), models: vi.fn(),
  user: { id: 7, role: 'admin' } as { id: number; role: string } | null,
  denied: [] as string[],
}))
vi.mock('../../api', () => ({
  printers: { list: h.printers }, jobs: { list: h.jobs }, printFiles: { any: h.files }, models: { list: h.models },
}))
vi.mock('../../permissions', () => ({
  getCurrentUser: () => h.user,
  canDo: (action: string) => !h.denied.includes(action),
  canAccessPage: (page: string) => !h.denied.includes('page:' + page),
}))

const online = () => ({ id: 1, name: 'P1', gcode_state: 'IDLE', last_seen: new Date().toISOString(), filament_slots: [{ filament_type: 'empty' }] })
const offline = () => ({ id: 1, name: 'P1', gcode_state: 'OFFLINE', last_seen: null, filament_slots: [{ filament_type: 'empty' }] })

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><GettingStartedChecklist /></MemoryRouter></QueryClientProvider>)
}
const card = () => screen.findByRole('region', { name: 'Getting started' })
const step = (root: HTMLElement, id: string) => root.querySelector(`[data-step="${id}"]`) as HTMLElement
const done = (root: HTMLElement) => Array.from(root.querySelectorAll('[data-step]')).filter(li => li.getAttribute('data-done') === 'true').map(li => li.getAttribute('data-step'))
/** Lets the three reads finish when no card is expected. */
async function settle() {
  await waitFor(() => { expect(h.printers).toHaveBeenCalled(); expect(h.jobs).toHaveBeenCalled(); expect(h.files).toHaveBeenCalled() })
  await new Promise(resolve => setTimeout(resolve, 20))
}

beforeEach(() => {
  localStorage.clear()
  h.user = { id: 7, role: 'admin' }
  h.denied = []
  h.printers.mockReset().mockResolvedValue([])
  h.jobs.mockReset().mockResolvedValue([])
  h.files.mockReset().mockResolvedValue(false)
  h.models.mockReset().mockResolvedValue([])
})
afterEach(() => cleanup())

describe('Getting started checklist: lines tick from real state', () => {
  it('a fresh install shows five lines, none done, each linking to where it is done', async () => {
    show()
    const root = await card()
    expect(done(root)).toEqual([])
    expect(within(root).getByText('0 of 5 done')).toBeInTheDocument()
    const links = Object.fromEntries(within(root).getAllByRole('link').map(a => [a.textContent, a.getAttribute('href')]))
    expect(links).toEqual({
      'Add a printer': '/printers',
      'See a printer online': '/printers',
      'Upload a print file': '/upload',
      'Set the filament in a printer slot': '/printers',
      'Send your first job to a printer': '/jobs',
    })
  })

  it('an added printer that is offline ticks only "Add a printer"', async () => {
    h.printers.mockResolvedValue([offline()])
    show()
    expect(done(await card())).toEqual(['printer-added'])
  })

  it('an online printer ticks "See a printer online"', async () => {
    h.printers.mockResolvedValue([online()])
    show()
    expect(done(await card())).toEqual(['printer-added', 'printer-online'])
  })

  it('an uploaded file ticks the upload line without reading the model list', async () => {
    h.files.mockResolvedValue(true)
    show()
    expect(done(await card())).toEqual(['file-uploaded'])
    expect(h.models).not.toHaveBeenCalled()
  })

  it('a model with no uploaded file ticks the upload line too', async () => {
    h.models.mockResolvedValue([{ id: 3, name: 'Bracket' }])
    show()
    expect(done(await card())).toEqual(['file-uploaded'])
  })

  it('filament on one slot ticks the filament line; an empty slot does not', async () => {
    h.printers.mockResolvedValue([{ ...offline(), filament_slots: [{ filament_type: 'empty' }, { filament_type: 'PLA' }] }])
    show()
    expect(done(await card())).toEqual(['printer-added', 'filament-set'])
  })

  it('a job that reached a printer ticks the last line, asking only for sent jobs', async () => {
    h.jobs.mockResolvedValue([{ id: 5, status: 'completed' }])
    show()
    expect(done(await card())).toEqual(['job-sent'])
    expect(h.jobs).toHaveBeenCalledWith(['printing', 'paused', 'completed', 'failed'], null, { limit: 1 })
  })

  it('a done line is no longer a link', async () => {
    h.printers.mockResolvedValue([offline()])
    show()
    const root = await card()
    expect(within(step(root, 'printer-added')).queryByRole('link')).toBeNull()
    expect(within(step(root, 'printer-added')).getByText('Done:')).toBeInTheDocument()
  })
})

describe('Getting started checklist: when it is shown', () => {
  const everything = () => {
    h.printers.mockResolvedValue([{ ...online(), filament_slots: [{ filament_type: 'PETG' }] }])
    h.files.mockResolvedValue(true)
    h.jobs.mockResolvedValue([{ id: 5, status: 'printing' }])
  }

  it('disappears when every line is done, and stays gone when a printer later goes offline', async () => {
    everything()
    show(); await settle()
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    await waitFor(() => expect(localStorage.getItem('odin_getting_started_dismissed:7')).toBe('true'))
    cleanup()
    h.printers.mockResolvedValue([offline()])
    h.printers.mockClear()
    show()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    expect(h.printers).not.toHaveBeenCalled()
  })

  it('Dismiss hides it for that user, but not for another user', async () => {
    show()
    fireEvent.click(within(await card()).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    expect(localStorage.getItem('odin_getting_started_dismissed:7')).toBe('true')
    cleanup()
    show()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    cleanup()
    h.user = { id: 8, role: 'operator' }
    show()
    expect(await card()).toBeInTheDocument()
  })

  it('honours the old tour flag: anyone who finished or skipped the tour never sees it', async () => {
    localStorage.setItem('odin_onboarding_complete', 'true')
    show()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    expect(h.printers).not.toHaveBeenCalled()
    expect(h.jobs).not.toHaveBeenCalled()
  })

  it('honours the old per-user tour flag for that user only', async () => {
    localStorage.setItem('odin_onboarding_complete:7', 'true')
    show()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    cleanup()
    h.user = { id: 8, role: 'admin' }
    show()
    expect(await card()).toBeInTheDocument()
  })

  it('a viewer sees nothing and nothing is read for them', async () => {
    h.user = { id: 7, role: 'viewer' }
    show()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
    expect(h.printers).not.toHaveBeenCalled()
    expect(h.files).not.toHaveBeenCalled()
  })

  it('does not show before the user is known', async () => {
    h.user = null
    show()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
  })

  it('does not show when the printer list cannot be read', async () => {
    h.printers.mockRejectedValue(new Error('403'))
    show(); await settle()
    expect(screen.queryByRole('region', { name: 'Getting started' })).toBeNull()
  })

  it('leaves out a line whose state cannot be read instead of guessing', async () => {
    h.jobs.mockRejectedValue(new Error('403'))
    show()
    const root = await card()
    expect(step(root, 'job-sent')).toBeNull()
    expect(within(root).getByText('0 of 4 done')).toBeInTheDocument()
  })

  it('a step the user may not do is shown without a link', async () => {
    h.user = { id: 9, role: 'operator' }
    h.denied = ['printers.add']
    show()
    const line = step(await card(), 'printer-added')
    expect(within(line).queryByRole('link')).toBeNull()
    expect(line).toHaveTextContent('Add a printer (ask an administrator)')
  })

  it('without the Upload page the upload line links to Models', async () => {
    h.denied = ['page:upload']
    show()
    expect(within(await card()).getByRole('link', { name: 'Upload a print file' })).toHaveAttribute('href', '/models')
  })
})

describe('Getting started checklist: wording', () => {
  it('names nothing a Community licence lacks (teams, orders, analytics, paid tiers)', async () => {
    show()
    const text = (await card()).textContent || ''
    expect(text).not.toMatch(/team|user|invite|order|product|analytic|report|maintenance|pro\b|upgrade|licen[cs]e|camera|vigil/i)
  })
})

describe('Getting started checklist: where it lives', () => {
  it('is on both dashboards, and the fixed tour is gone from the app shell', () => {
    expect(classicDashboard).toContain('<GettingStartedChecklist />')
    expect(floor).toContain('<GettingStartedChecklist />')
    expect(app).not.toMatch(/OnboardingTour/)
  })
})
