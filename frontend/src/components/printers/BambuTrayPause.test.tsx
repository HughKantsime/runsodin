import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TraySettings from './TraySettings'
import PauseCause from './PauseCause'
import PrinterCard from './PrinterCard'
import { fetchAPI } from '../../api/client'
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({id:1,role:'admin'}) }))
afterEach(cleanup)
beforeEach(() => vi.mocked(fetchAPI).mockReset())

test('reported preset supplies exact id, type, temperatures and local slot without k', async () => {
  vi.mocked(fetchAPI).mockResolvedValueOnce([{ preset_id:'GFG02',material:'PETG',nozzle_temp_min:230,nozzle_temp_max:260 }]).mockResolvedValueOnce({result:'success',reason:null})
  render(<TraySettings printerId={9} />)
  fireEvent.click(screen.getByText('Configure AMS tray'))
  fireEvent.change(await screen.findByLabelText('Reported preset'), {target:{value:'0'}})
  fireEvent.change(screen.getByLabelText('AMS'), {target:{value:'2'}})
  fireEvent.change(screen.getByLabelText('Slot'), {target:{value:'1'}})
  fireEvent.click(screen.getByText('Set tray'))
  await screen.findByText('Printer result: success')
  expect(fetchAPI).toHaveBeenLastCalledWith('/printers/9/ams/2/slots/1', {
    method:'PUT',body:JSON.stringify({preset_id:'GFG02',material:'PETG',nozzle_temp_min:230,nozzle_temp_max:260,color:'#FFFFFF'}),
  })
})

test('manual entry stays required and printer failure is visible', async () => {
  vi.mocked(fetchAPI).mockResolvedValueOnce([]).mockRejectedValueOnce(new Error('Printer refused: busy'))
  render(<TraySettings printerId={9} />)
  fireEvent.click(screen.getByText('Configure AMS tray'))
  expect(screen.getByLabelText('preset_id')).toBeRequired()
  await waitFor(() => expect(fetchAPI).toHaveBeenCalledTimes(1))
  for (const [field,value] of Object.entries({preset_id:'GFG02',material:'PETG',nozzle_temp_min:'230',nozzle_temp_max:'260'})) fireEvent.change(screen.getByLabelText(field),{target:{value}})
  fireEvent.click(screen.getByText('Set tray'))
  await screen.findByText('Printer refused: busy')
})

test('paused display never falls back to a stale last error', () => {
  const printer:any = { gcode_state:'PAUSE',print_stage:'Paused: AMS runout',last_error_message:'Stale fan error' }
  const view=render(<PauseCause printer={printer} />)
  expect(screen.getByText('Paused: AMS runout')).toBeInTheDocument()
  expect(screen.queryByText('Stale fan error')).not.toBeInTheDocument()
  view.rerender(<PauseCause printer={{...printer,print_stage:'Paused'}} />)
  expect(screen.getByText('Paused: Cause unavailable')).toBeInTheDocument()
  view.rerender(<PauseCause printer={{...printer,gcode_state:'RUNNING'}} />)
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})

test('printer card renders current pause cause', () => {
  const printer:any={id:9,name:'Synthetic',api_type:'bambu',gcode_state:'PAUSE',print_stage:'Paused: AMS runout',filament_slots:[],tags:[],last_seen:new Date().toISOString()}
  render(<QueryClientProvider client={new QueryClient()}><PrinterCard printer={printer} onEdit={vi.fn()} onDelete={vi.fn()} onToggleActive={vi.fn()} onSyncAms={vi.fn()} /></QueryClientProvider>)
  expect(screen.getByText('Paused: AMS runout')).toBeInTheDocument()
})
