import { afterEach, expect, it } from 'vitest'
import { SUPPORT_LEVELS, PRINTER_TYPE_OPTIONS, experimentalWarning, printerTypeLabel, supportLevelOfPrinter, supportLevelOfType } from '../supportLevels'

const original = { ...SUPPORT_LEVELS }
afterEach(() => { for (const key of Object.keys(SUPPORT_LEVELS)) delete SUPPORT_LEVELS[key]; Object.assign(SUPPORT_LEVELS, original) })

it('maps current types, manual printers and unknown types', () => {
  expect(supportLevelOfType('bambu')).toBe('supported')
  expect(supportLevelOfType(' Moonraker ')).toBe('supported')
  expect(supportLevelOfType(null)).toBeNull()
  expect(supportLevelOfType('made_up')).toBe('unsupported')
})

it('prefers the level the server reports', () => {
  expect(supportLevelOfPrinter({ api_type: 'bambu', support_level: 'experimental' })).toBe('experimental')
  expect(supportLevelOfPrinter({ api_type: 'bambu', support_level: 'bogus' })).toBe('supported')
})

it('labels an Experimental type in the add-printer list (table marked in the test only)', () => {
  const moonraker = PRINTER_TYPE_OPTIONS.find(o => o.value === 'moonraker')!
  expect(printerTypeLabel(moonraker)).toBe('Klipper / Moonraker')
  SUPPORT_LEVELS.moonraker = 'experimental'
  expect(printerTypeLabel(moonraker)).toBe('Klipper / Moonraker — Experimental')
})

it('uses the spec warning for OctoPrint and a generic one otherwise', () => {
  expect(experimentalWarning('octoprint')).toBe("Built from OctoPrint's public API and not yet verified on real hardware. Sending jobs and controls may not work as expected. Send printer diagnostics if something is wrong.")
  expect(experimentalWarning('other')).toMatch(/^Built from public documentation and not yet verified on real hardware\./)
})
