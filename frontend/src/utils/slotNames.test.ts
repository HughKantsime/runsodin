import { describe, expect, it } from 'vitest'
import { getShortName, materialLabel, slotColorName, slotFullName } from './shared'

const slot = (filament_type: string, color = '', extra = {}) => ({ filament_type, color, ...extra }) as never

describe('filament slot names', () => {
  it('writes material codes the way people do', () => {
    expect(materialLabel('PLA')).toBe('PLA')
    expect(materialLabel('PLA_SUPPORT')).toBe('PLA Support')
    expect(materialLabel('PETG_CF')).toBe('PETG-CF')
    expect(materialLabel('NYLON_GF')).toBe('NYLON-GF')
    expect(materialLabel('SOME_NEW_TYPE')).toBe('SOME NEW TYPE')
    expect(materialLabel('empty')).toBe('Empty')
    expect(materialLabel(null)).toBe('Empty')
  })
  it('cleans the colour name: no preset prefix, brand or hex code', () => {
    expect(slotColorName('preset: Bambu Lab PLA Basic Jade White')).toBe('PLA Basic Jade White')
    expect(slotColorName('Polymaker Galaxy Black')).toBe('Galaxy Black')
    expect(slotColorName('#FF0000')).toBe('')
    expect(slotColorName('68724D')).toBe('')
    expect(slotColorName(slot('PLA', 'Review Blue Long Name Filament'))).toBe('Review Blue Long Name Filament')
  })
  it('a tile shows the material, never a cut-off colour name', () => {
    expect(getShortName(slot('PLA', 'Review Blue Long Name Filament'))).toBe('PLA')
    expect(getShortName(slot('PLA_SUPPORT', 'Bambu Lab Support for PLA'))).toBe('PLA Support')
    expect(getShortName(slot('empty'))).toBe('Empty')
    expect(getShortName(slot('', 'Galaxy Black'))).toBe('Galaxy Black')
    expect(getShortName(slot('PLA', 'PLA'))).toBe('PLA')
  })
  it('the full name joins material and colour without repeating itself', () => {
    expect(slotFullName(slot('PLA', 'Polymaker Galaxy Black'))).toBe('PLA · Galaxy Black')
    expect(slotFullName(slot('PLA', 'PLA'))).toBe('PLA')
    expect(slotFullName(slot('PLA_SUPPORT', 'Bambu Lab Support for PLA'))).toBe('PLA Support')
    expect(slotFullName(slot('PLA', '000000'))).toBe('PLA')
    expect(slotFullName(slot('empty'))).toBe('Empty')
  })
})
