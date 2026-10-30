import { describe, expect, it } from 'vitest'
import { supportsFilamentMapping } from './printerMapping'

describe('supportsFilamentMapping', () => {
  it('offers mapping on Bambu whatever the model', () => {
    expect(supportsFilamentMapping({ api_type: 'bambu' })).toBe(true)
    expect(supportsFilamentMapping({ api_type: 'bambu', model: 'A1' })).toBe(true)
  })

  it.each([
    { model: 'Kobra S1' },
    { model: 'Anycubic Kobra S1 Combo' },
    { model: 'kobra_s1' },
    { model: 'KOBRA-S1' },
    { model: 'Unknown', machine_type: 'Kobra S1' },
  ])('offers mapping on a Moonraker Kobra S1: %j', identity => {
    expect(supportsFilamentMapping({ api_type: 'moonraker', ...identity })).toBe(true)
  })

  it.each([
    { api_type: 'moonraker' },
    { api_type: 'moonraker', model: 'Voron 2.4' },
    { api_type: 'moonraker', model: 'Kobra 3' },
    { api_type: 'moonraker', model: 'Kobra S10' },
    { api_type: 'moonraker', model: null, machine_type: null },
    { api_type: 'prusalink', model: 'Kobra S1' },
    { api_type: 'anycubic_lan', model: 'Kobra S1' },
    {},
  ])('hides mapping otherwise: %j', printer => {
    expect(supportsFilamentMapping(printer)).toBe(false)
  })

  it('hides mapping without a printer', () => {
    expect(supportsFilamentMapping()).toBe(false)
  })
})
