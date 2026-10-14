import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { I18nProvider, useTranslation } from './I18nContext'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../api', () => ({ fetchAPI: api.request }))
function Probe() {
  const { t, locale, setLocale } = useTranslation()
  return <><output>{locale}</output><p>{t('nav.dashboard')}</p><button onClick={() => setLocale('de')}>Deutsch</button></>
}
beforeEach(() => { localStorage.clear(); api.request.mockReset() })
afterEach(cleanup)
it('late initial server response cannot override a newer explicit choice', async () => {
  let resolveInitial: (value: any) => void
  api.request.mockImplementation((_url, options) => options ? Promise.resolve({ language: 'de' }) : new Promise(resolve => { resolveInitial = resolve }))
  render(<I18nProvider><Probe /></I18nProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'Deutsch' }))
  await act(async () => { resolveInitial!({ language: 'ja' }) })
  expect(screen.getByText('Übersicht')).toBeInTheDocument()
  expect(localStorage.getItem('odin-locale')).toBe('de')
})
it('initial server response after unmount cannot change browser preference', async () => {
  let resolveInitial: (value: any) => void
  api.request.mockImplementation(() => new Promise(resolve => { resolveInitial = resolve }))
  const view = render(<I18nProvider><Probe /></I18nProvider>); view.unmount()
  await act(async () => { resolveInitial!({ language: 'ja' }) })
  expect(localStorage.getItem('odin-locale')).toBeNull()
})
it.each(['unsupported', '__proto__', 'constructor'])('invalid stored locale %s falls back to English before server loads', invalid => {
  localStorage.setItem('odin-locale', invalid); api.request.mockReturnValue(new Promise(() => {}))
  render(<I18nProvider><Probe /></I18nProvider>)
  expect(screen.getByText('Dashboard')).toBeInTheDocument(); expect(screen.getByRole('status')).toHaveTextContent('en')
})
it('components outside provider use English catalog', () => {
  render(<Probe />); expect(screen.getByText('Dashboard')).toBeInTheDocument()
})
