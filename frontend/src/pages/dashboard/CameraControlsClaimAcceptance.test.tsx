import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Overlay from './Overlay'
import PrinterCard from '../../components/printers/PrinterCard'
vi.mock('../../permissions',()=>({canDo:()=>false,getCurrentUser:()=>null}))
const sample = {printer_name:'Synthetic OBS printer',job_name:'Synthetic OBS job',gcode_state:'RUNNING',print_progress:45,time_remaining_min:75,current_layer:12,total_layers:100,nozzle_temp:210.4,nozzle_target_temp:220,bed_temp:59.6,bed_target_temp:60,camera_url:'/synthetic-camera.jpg'}
let data:any
beforeEach(()=>{
 vi.useFakeTimers();data={...sample}
 vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>data})))
})
afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()})
async function mount(query=''){
 const view=render(<MemoryRouter initialEntries={['/overlay/19'+query]}><Routes><Route path="/overlay/:printerId" element={<Overlay/>}/></Routes></MemoryRouter>)
 await act(async()=>{await Promise.resolve();await Promise.resolve()})
 return view
}
test('F02-2.8-01/03 overlay renders from public printer-id route without an auth provider',async()=>{
 await mount()
 expect(fetch).toHaveBeenCalledWith('/api/overlay/19')
 expect(screen.getByTestId('overlay-printer-name')).toHaveTextContent('Synthetic OBS printer')
 expect(screen.getByTestId('overlay-job-name')).toHaveTextContent('Synthetic OBS job')
 expect(screen.getByTestId('overlay-status')).toHaveTextContent('Printing')
 expect(screen.getByTestId('overlay-progress')).toHaveTextContent('45%')
 expect(screen.getByText('ETA 1h 15m')).toBeInTheDocument()
 expect(screen.getByText('Layer 12 / 100')).toBeInTheDocument()
 expect(screen.getByText('Nozzle')).toBeInTheDocument();expect(screen.getByText('Bed')).toBeInTheDocument()
 expect(screen.getByText('210°C')).toBeInTheDocument();expect(screen.getByText('60°C')).toBeInTheDocument()
})
test('F02-2.8-02 camera image and status panel declare full-screen 70/30 dark structure',async()=>{
 const view=await mount()
 expect(view.container.firstElementChild).toHaveClass('h-screen','w-screen','bg-black')
 const image=screen.getByRole('img',{name:'Camera stream'})
 expect(image).toHaveAttribute('src','/synthetic-camera.jpg')
 expect(image.parentElement).toHaveClass('flex-[7]')
 expect(screen.getByTestId('overlay-printer-name').parentElement?.parentElement).toHaveClass('flex-[3]')
})
test('F02-2.8-04 polls after exactly five seconds, updates status and clears timer on unmount',async()=>{
 const view=await mount();expect(fetch).toHaveBeenCalledTimes(1)
 await act(async()=>{vi.advanceTimersByTime(4999)})
 expect(fetch).toHaveBeenCalledTimes(1)
 data={...sample,gcode_state:'PAUSE',print_progress:60}
 await act(async()=>{vi.advanceTimersByTime(1)})
 expect(fetch).toHaveBeenCalledTimes(2)
 expect(screen.getByTestId('overlay-status')).toHaveTextContent('Paused')
 expect(screen.getByTestId('overlay-progress')).toHaveTextContent('60%')
 view.unmount();await act(async()=>{vi.advanceTimersByTime(10000)})
 expect(fetch).toHaveBeenCalledTimes(2)
 expect(vi.getTimerCount()).toBe(0)
})
test('F02-2.8-05 camera=false removes camera but retains printer telemetry',async()=>{
 await mount('?camera=false')
 expect(screen.queryByRole('img',{name:'Camera stream'})).not.toBeInTheDocument()
 expect(screen.getByTestId('overlay-progress')).toHaveTextContent('45%')
})
test.each([['dark','bg-black'],['light','bg-white']])('F02-2.8-05 theme=%s selects declared background',async(theme,klass)=>{
 const view=await mount('?theme='+theme)
 expect(view.container.firstElementChild).toHaveClass(klass)
})
test('F02-2.8-03 idle hides active job/progress while retaining identity and temperatures',async()=>{
 data={...sample,gcode_state:'IDLE'};await mount()
 expect(screen.getByTestId('overlay-status')).toHaveTextContent('Idle')
 expect(screen.queryByTestId('overlay-job-name')).not.toBeInTheDocument()
 expect(screen.queryByTestId('overlay-progress')).not.toBeInTheDocument()
 expect(screen.getByText('Nozzle')).toBeInTheDocument()
})
test('F02-2.8-01 initial failed overlay fetch presents unavailable rather than invented telemetry',async()=>{
 vi.mocked(fetch).mockResolvedValue({ok:false,status:404} as Response)
 await mount()
 expect(screen.getByText('Printer not found or unavailable')).toBeInTheDocument()
 expect(screen.queryByTestId('overlay-progress')).not.toBeInTheDocument()
})
test('F02-2.8-06 printer-card copy action writes correct OBS overlay URL',()=>{
 const writeText=vi.fn().mockResolvedValue(undefined)
 const descriptor=Object.getOwnPropertyDescriptor(navigator,'clipboard')
 Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText}})
 try{
 const printer:any={id:19,name:'Synthetic OBS printer',nickname:null,model:null,is_active:true,api_type:null,api_host:null,has_api_key:false,camera_url:null,bed_temp:null,bed_target_temp:null,nozzle_temp:null,nozzle_target_temp:null,gcode_state:'IDLE',print_stage:null,lights_on:null,filament_slots:[],tags:[],machine_type:null}
 render(<PrinterCard printer={printer} allFilaments={[]} spools={[]} onDelete={vi.fn()} onToggleActive={vi.fn()} onUpdateSlot={vi.fn()} onEdit={vi.fn()} onSyncAms={vi.fn()}/>)
 fireEvent.click(screen.getByRole('button',{name:'Copy OBS overlay URL'}))
 expect(writeText).toHaveBeenCalledExactlyOnceWith(`${window.location.origin}/overlay/19`)
 }finally{if(descriptor)Object.defineProperty(navigator,'clipboard',descriptor);else delete (navigator as any).clipboard}
})
