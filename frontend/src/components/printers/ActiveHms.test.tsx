import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import ActiveHms from './ActiveHms'
import { hmsEvents, hmsFaults } from '../../utils/hms'
import { printerBadgeStatus } from '../ui/StatusBadge'

const notice = {code:'05010400_00030002',raw_code:0x30002,severity:'info',message:'Lubricate threaded rods',wiki_url:'https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0501_0400_0003_0002'}
describe('active Bambu notices', () => {
 it('excludes safety records from active text, fleet fault counts and badges', () => {
  const safety = [{display:false,hms_safety:{attr:0x05000300,code:0x20018}}, {display:false,hms_safety:null}]
  render(<ActiveHms printer={{api_type:'bambu',hms_errors:JSON.stringify([...safety,notice])}} />)
  expect(hmsEvents([...safety,notice])).toEqual([notice])
  expect(hmsFaults(JSON.stringify(safety))).toEqual([])
  expect(printerBadgeStatus({gcode_state:'FINISH',hms_errors:safety},true)).toBe('FINISH')
  expect(screen.getByText('Lubricate threaded rods')).toBeInTheDocument()
  expect(screen.queryByText('Active faults')).not.toBeInTheDocument()
 })
 it('shows one maintenance notice, a wiki link and explicitly separate history', () => {
  render(<ActiveHms printer={{api_type:'bambu',hms_errors:JSON.stringify([notice,notice]),last_error_code:'old',last_error_message:'Stale AMS fault'}} />)
  expect(screen.getByText('Maintenance due / notices')).toBeInTheDocument()
  expect(screen.getAllByText('Lubricate threaded rods')).toHaveLength(1)
  expect(screen.getByRole('link')).toHaveAttribute('href',notice.wiki_url)
  expect(screen.getByText('Last seen code (history)')).toBeInTheDocument()
  expect(screen.queryByText('Stale AMS fault')).not.toBeInTheDocument()
  expect(screen.queryByText('Active faults')).not.toBeInTheDocument()
 })
 it('clears active notices without turning historical codes into faults', () => {
  const {rerender}=render(<ActiveHms printer={{api_type:'bambu',hms_errors:[notice]}} />)
  rerender(<ActiveHms printer={{api_type:'bambu',hms_errors:'[]',last_error_code:notice.code}} />)
  expect(screen.queryByText('Lubricate threaded rods')).not.toBeInTheDocument()
  expect(screen.getByText('No active printer notices.')).toBeInTheDocument()
 })
 it('keeps common/info out of fleet fault counts and badges', () => {
  expect(hmsFaults([notice,{code:0x40001}])).toEqual([])
  expect(printerBadgeStatus({gcode_state:'FINISH',hms_errors:[notice]},true)).toBe('FINISH')
  expect(hmsFaults([{code:0x10001},{code:0x20001}])).toHaveLength(2)
  expect(hmsFaults('[]')).toEqual([])
 })
})
