import type { ReceiveRolls } from '../../api/inventory'

// RFC 4180 quoting, including commas, escaped quotes and newlines in notes.
export function csvRecords(source: string): string[][] {
  const rows: string[][] = [], row: string[] = []
  let cell = '', quoted = false, closed = false
  for (let i = 0; i < source.length; i++) {
    const c = source[i]
    if (quoted) {
      if (c === '"' && source[i + 1] === '"') { cell += '"'; i++ }
      else if (c === '"') { quoted = false; closed = true }
      else cell += c
    } else if (c === ',' || c === '\n' || c === '\r') {
      row.push(cell.trim()); cell = ''; closed = false
      if (c !== ',') { if (row.some(Boolean)) rows.push([...row]); row.length = 0; if (c === '\r' && source[i + 1] === '\n') i++ }
    } else if (c === '"' && !cell && !closed) quoted = true
    else { if (closed || c === '"') throw new Error('Invalid CSV quoting'); cell += c }
  }
  if (quoted) throw new Error('Unclosed CSV quote')
  row.push(cell.trim()); if (row.some(Boolean)) rows.push(row)
  return rows
}
const materials = new Set('PLA PLA-S PLA-CF PLA-SILK PLA-MATTE PLA-AERO PETG PETG-CF ABS ABS-GF ASA TPU TPU-95A TPU-83A PA PA-CF PA-GF PA6 PA6-CF PA6-GF PA12-CF PC PC-ABS PC-CF PVA HIPS SUPPORT SUPPORT-W SUPPORT-G'.split(' '))
const fields = ['brand', 'product_line', 'material', 'color_name', 'color_hex', 'quantity', 'initial_weight_g', 'spool_weight_g', 'storage_location', 'sealed', 'vendor', 'note']
export const csvExample = 'brand,product_line,material,color_name,quantity,storage_location\nBambu Lab,PLA Matte,PLA,Nardo Gray,1,Shelf A'
export interface PreviewRow { row: ReceiveRolls; errors: string[]; newProduct: boolean }
export function previewCsv(source: string, library: { brand?: string; name?: string; material?: string }[]): PreviewRow[] {
  const [header, ...records] = csvRecords(source.replace(/^\uFEFF/, ''))
  if (!header || new Set(header).size !== header.length || header.some(h => !fields.includes(h))) throw new Error('Use the CSV column names shown below, with no duplicate columns.')
  if (!records.length || records.length > 200) throw new Error('Include between 1 and 200 rows.')
  const result = records.map((cells, i) => {
    const values = Object.fromEntries(header.map((h, n) => [h, cells[n] || '']))
    const errors: string[] = []
    if (cells.length !== header.length) errors.push('Column count differs from header')
    for (const key of ['brand', 'product_line', 'material', 'storage_location']) if (!values[key]) errors.push(`${key} is required`)
    const sealed = values.sealed !== 'false'
    if (values.sealed && !['true', 'false'].includes(values.sealed)) errors.push('sealed must be true or false')
    if (sealed && !values.color_name) errors.push('Color name is required for sealed stock')
    const material = values.material?.toUpperCase().replace(/_/g, '-') || ''
    if (!materials.has(material)) errors.push(`Unknown material: ${material || '(blank)'}`)
    const number = (key: string, fallback: number, min: number, max: number, integer = false) => {
      const value = values[key] ? Number(values[key]) : fallback
      if (!Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) errors.push(`Invalid ${key}`)
      return value
    }
    const color = values.color_hex?.replace(/^#/, '').toUpperCase()
    if (color && !/^[0-9A-F]{6}$/.test(color)) errors.push('Color hex must have six digits')
    for (const key of ['brand', 'product_line', 'color_name', 'storage_location', 'vendor']) if (values[key]?.length > 100) errors.push(`${key} is too long`)
    if (values.note?.length > 2000) errors.push('Note is too long')
    const row: ReceiveRolls = { brand: values.brand, product_line: values.product_line, material,
      color_name: values.color_name || undefined, color_hex: color || undefined,
      quantity: number('quantity', 1, 1, 100, true), initial_weight_g: number('initial_weight_g', 1000, 0.001, 100000),
      spool_weight_g: number('spool_weight_g', 250, 0, 10000), storage_location: values.storage_location,
      sealed, vendor: values.vendor || undefined, note: values.note || undefined }
    const same = (a?: string, b?: string) => a?.trim().toLowerCase() === b?.trim().toLowerCase()
    return { row, errors: errors.map(e => `Row ${i + 1}: ${e}`), newProduct: !library.some(f => same(f.brand, row.brand) && same(f.name, row.product_line) && same(f.material, material)) }
  })
  if (result.reduce((total, r) => total + r.row.quantity, 0) > 1000) throw new Error('Receive at most 1000 rolls per import.')
  return result
}
