import { describe, expect, test } from 'vitest'
import { HELP_LINKS, getHelpLink } from './helpLinks'

describe('getHelpLink', () => {
  test('exact route wins', () => {
    expect(getHelpLink('/printers')).toBe(HELP_LINKS['/printers'])
    expect(getHelpLink('/')).toBe(HELP_LINKS['/'])
  })
  test('a printer detail route reaches the printer controls page, not the printer list page', () => {
    expect(getHelpLink('/printers/7')?.url).toBe('https://docs.runsodin.com/features/mid-print-controls')
    expect(getHelpLink('/printers/7/settings')?.url).toBe('https://docs.runsodin.com/features/mid-print-controls')
  })
  test('sub-routes without their own key fall back to the longest matching prefix', () => {
    expect(getHelpLink('/cameras/3')).toBe(HELP_LINKS['/cameras'])
    expect(getHelpLink('/settings')).toBe(HELP_LINKS['/settings'])
  })
  test('prefixes match whole segments only, and "/" never matches as a prefix', () => {
    expect(getHelpLink('/printersx')).toBeNull()
    expect(getHelpLink('/unknown/page')).toBeNull()
  })
  test('Settings and Timeline link their own docs pages', () => {
    expect(getHelpLink('/settings')?.url).toBe('https://docs.runsodin.com/configuration/settings')
    expect(getHelpLink('/timeline')?.url).toBe('https://docs.runsodin.com/features/timeline')
  })
  test('no dead keys for routes the app does not have', () => {
    expect(HELP_LINKS['/settings/branding']).toBeUndefined()
    expect(HELP_LINKS['/printers/detail']).toBeUndefined()
  })
})
