import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import ArchiveMaterial from './ArchiveMaterial'
afterEach(cleanup)

it.each([['PLA','spool','PLA'], ['PLA,PETG,ABS,TPU','file','PLA + PETG + ABS +1 (from file)'], ['PETG','job','PETG (from job)']] as const)('shows %s with its source', (names,source,expected) => {
  const { container } = render(<ArchiveMaterial archive={{materials:names.split(',').map(material => ({material,grams:null})),material_source:source}} />)
  expect(container).toHaveTextContent(expected)
})
it('explains missing material', () => {
  render(<ArchiveMaterial archive={{materials:[], material_source:null}} />)
  expect(screen.getByTitle('Not recorded for this print')).toHaveTextContent('—')
})
