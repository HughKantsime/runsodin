import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { OrgProvider } from '../../contexts/OrgContext'
import type { SystemNotice, UpdateStatus } from '../../api/system'
import SystemNoticesBanner from './SystemNoticesBanner'
import UpdateSettings from './UpdateSettings'

const disclosure = "When this is on, ODIN asks GitHub once a day for the list of ODIN releases (api.github.com/repos/HughKantsime/runsodin/releases). ODIN sends nothing about this server: no licence, no identifier, no usage data. GitHub sees this server's public IP address, as any website does. When this is off, ODIN makes no such request."
const off = "Off. ODIN does not check for updates. To hear about releases without this, watch the repository's releases and security advisories on GitHub."
const switchName = 'Check for new ODIN versions'
let state: UpdateStatus
let notices: SystemNotice[]
const requests = vi.fn()

beforeEach(() => {
  state = {enabled: false, stored_enabled: false, disabled_reason: null, installed: '2.5.0', newest: null, published_at: null,
    checked_at: null, outcome: null, available: false, security: false}
  notices = []
  requests.mockImplementation(async (url: string, init?: RequestInit) => {
    let body: unknown
    if (url === '/api/orgs') body = []
    else if (url === '/api/system/updates') {
      if (init?.method === 'PUT') {
        state.stored_enabled = JSON.parse(String(init.body)).enabled
        state.enabled = state.stored_enabled && !state.disabled_reason
      }
      body = state
    } else if (url === '/api/system/updates/check') {
      Object.assign(state, {outcome: 'ok', available: false, newest: '2.5.0', checked_at: '2026-11-20T12:00:00Z'})
      body = state
    } else if (url === '/api/system/notices') body = {notices}
    else if (url === '/api/system/notices/update_available%3A3.0.0/dismiss' && init?.method === 'POST') {
      notices = []
      body = {status: 'dismissed'}
    } else throw new Error('Unexpected URL ' + url)
    return new Response(JSON.stringify(body), {headers: {'Content-Type': 'application/json'}})
  })
  vi.stubGlobal('fetch', requests)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); requests.mockReset() })

function open(role = 'admin', group_id: number | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], {id: 1, role, group_id})
  render(<QueryClientProvider client={client}><OrgProvider><MemoryRouter>
    <UpdateSettings /><SystemNoticesBanner />
  </MemoryRouter></OrgProvider></QueryClientProvider>)
  return client
}

test('administrator can opt in, check now and opt out with the disclosure always visible', async () => {
  open()
  expect(await screen.findByText(off)).toBeVisible()
  const toggle = screen.getByRole('switch', {name: switchName})
  expect(toggle).not.toBeChecked()
  expect(screen.getByText(disclosure)).toBeVisible()
  expect(screen.queryByRole('button', {name: 'Check now'})).toBeNull()
  fireEvent.click(toggle)
  expect(await screen.findByText('No check yet')).toBeVisible()
  expect(toggle).toBeChecked()
  fireEvent.click(screen.getByRole('button', {name: 'Check now'}))
  expect(await screen.findByText('Up to date')).toBeVisible()
  expect(screen.getByText('Installed version: 2.5.0')).toBeVisible()
  expect(screen.getByText('Newest released version: 2.5.0')).toBeVisible()
  expect(screen.getByText(disclosure)).toBeVisible()
  fireEvent.click(toggle)
  expect(await screen.findByText(off)).toBeVisible()
})

test.each(['Update checks are turned off while ITAR mode is on: this server does not contact public services.',
           'Update checks are turned off by this server’s ODIN_UPDATE_CHECK setting.'])('disabled switch explains %s', async reason => {
  state.disabled_reason = reason
  open()
  expect(await screen.findByText(reason)).toBeVisible()
  expect(screen.getByRole('switch', {name: switchName})).toBeDisabled()
  expect(screen.getByText(disclosure)).toBeVisible()
  expect(screen.getByText(off)).toBeVisible()
})

test.each(['Update checks are turned off while ITAR mode is on: this server does not contact public services.',
           'Update checks are turned off by this server’s ODIN_UPDATE_CHECK setting.'])('withdraw a saved opt-in under %s', async reason => {
  Object.assign(state, {stored_enabled: true, disabled_reason: reason})
  open()
  expect(await screen.findByText(reason)).toBeVisible()
  expect(screen.getByText(/switched on earlier.*will resume/i)).toBeVisible()
  expect(screen.getByRole('switch', {name: switchName})).toBeDisabled()
  fireEvent.click(screen.getByRole('button', {name: 'Turn off'}))
  await waitFor(() => expect(requests).toHaveBeenCalledWith('/api/system/updates', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({enabled: false}),
  })))
  expect(await screen.findByText(off)).toBeVisible()
  expect(screen.queryByRole('button', {name: 'Turn off'})).toBeNull()
  expect(screen.getByText(reason)).toBeVisible()
})

test('a failed check shows the date and keeps the newest known version', async () => {
  Object.assign(state, {enabled: true, checked_at: '2026-11-20T01:02:03Z', newest: '3.0.0', outcome: 'timeout'})
  open()
  expect(await screen.findByText('Could not reach GitHub on ' + new Date(state.checked_at!).toLocaleString())).toBeVisible()
  expect(screen.getByText('Newest released version: 3.0.0')).toBeVisible()
})

function notice(version: string, security: boolean): SystemNotice {
  return {kind: 'update_available', version, severity: security ? 'critical' : 'warning', dismissible: !security,
    title: `Version ${version} is available${security ? ' and fixes a security problem' : ''}`, message: '',
    link: 'https://github.com/HughKantsime/runsodin/releases/tag/v' + version}
}

test.each(['too_large', 'invalid_response'])('%s identifies an unreadable answer and its date', async outcome => {
  Object.assign(state, {enabled: true, checked_at: '2026-11-20T01:02:03Z', outcome})
  open()
  expect(await screen.findByRole('status')).toHaveTextContent(
    "GitHub's answer could not be read on " + new Date(state.checked_at!).toLocaleString())
})

test('an unknown installed version explains why there is no comparison', async () => {
  Object.assign(state, {enabled: true, installed: 'dev', checked_at: '2026-11-20T01:02:03Z',
    outcome: 'unknown_installed_version'})
  open()
  expect(await screen.findByRole('status')).toHaveTextContent('ODIN could not determine the installed version.')
})

test('dismissal identifies the displayed version and a later security update stays visible', async () => {
  notices = [notice('3.0.0', false)]
  Object.assign(state, {enabled: true, newest: '3.0.0', available: true, outcome: 'ok', checked_at: '2026-11-20T12:00:00Z'})
  const client = open()
  expect(await screen.findByRole('alert')).toHaveTextContent('Version 3.0.0 is available')
  expect(await screen.findByRole('status')).toHaveTextContent('Version 3.0.0 is available')
  fireEvent.click(screen.getByRole('button', {name: 'Dismiss'}))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  notices = [notice('3.1.0', true)]
  Object.assign(state, {newest: '3.1.0', security: true})
  await client.invalidateQueries()
  expect(await screen.findByRole('alert')).toHaveTextContent('Version 3.1.0 is available and fixes a security problem')
  expect(screen.getByRole('status')).toHaveTextContent('Version 3.1.0 is available and fixes a security problem')
  expect(screen.getByRole('link', {name: 'Release notes'})).toHaveAttribute('href', notices[0].link)
  expect(screen.queryByRole('button', {name: 'Dismiss'})).toBeNull()
})

test.each([['admin', 7], ['operator', null], ['viewer', null]] as const)('%s with group %s sees neither surface', async (role, group) => {
  notices = [notice('3.0.0', true)]
  open(role, group)
  expect(screen.queryByRole('switch')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(requests).not.toHaveBeenCalled()
})
