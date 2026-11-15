import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import PrinterDetail from './PrinterDetail'
vi.mock('../../permissions', () => ({canDo: () => true, canAccessPage: () => false}))
vi.mock('../../components/printers/MaintenanceBanner', () => ({default: () => null}))
vi.mock('../../components/printers/DispatchRecoveryPanel', () => ({default: () => null}))
vi.mock('../../api/client', () => ({fetchAPI: vi.fn().mockResolvedValue([])}))
vi.mock('../../api', () => ({
  printers:{get:vi.fn().mockResolvedValue({id:9,name:'Synthetic paused printer',api_type:'bambu',gcode_state:'PAUSE',print_stage:'Paused: AMS runout',last_error_message:'Stale fan error',filament_slots:[{slot_number:1,filament_type:'ASA-CF',catalogue_name:'ASA-CF Invented long colour name',color_hex:'123456'}]})},
  printerTelemetry:{get:vi.fn().mockResolvedValue([])},printJobs:{list:vi.fn().mockResolvedValue([])},
}))
afterEach(cleanup)
test('printer detail renders pause cause and opens explicit tray configuration',async()=>{
 render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/printers/9']}><Routes><Route path="/printers/:id" element={<PrinterDetail/>}/></Routes></MemoryRouter></QueryClientProvider>)
 expect(await screen.findByText('Paused: AMS runout')).toBeInTheDocument()
 expect(screen.queryByText('Stale fan error')).not.toBeInTheDocument()
 fireEvent.click(screen.getByText('Configure AMS tray'))
 expect(screen.getByLabelText('preset_id')).toBeRequired()
})

test('Foundry opens recovery when following a restart question alert',async()=>{
 document.documentElement.dataset.themeFamily='foundry'
 try {
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/printers/9#dispatch-recovery']}><Routes><Route path="/printers/:id" element={<PrinterDetail/>}/></Routes></MemoryRouter></QueryClientProvider>)
  const summary=await screen.findByText('Printer status and recovery')
  expect(summary.closest('details')).toHaveAttribute('open')
 } finally {document.documentElement.removeAttribute('data-theme-family')}
})

test.each(['classic', 'foundry'])('%s printer detail shows catalogue names from the printer response', async family => {
 document.documentElement.dataset.themeFamily = family
 try {
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/printers/9']}><Routes><Route path="/printers/:id" element={<PrinterDetail/>}/></Routes></MemoryRouter></QueryClientProvider>)
  const name = await screen.findByText('ASA-CF Invented long colour name')
  expect(name).not.toHaveClass('truncate')
 } finally { document.documentElement.removeAttribute('data-theme-family') }
})
