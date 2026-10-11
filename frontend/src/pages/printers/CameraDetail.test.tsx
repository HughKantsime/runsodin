import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import CameraDetail from './CameraDetail'
import toast from 'react-hot-toast'
const state=vi.hoisted(()=>({printers:[] as any[],error:false,loading:false,vision:{enabled:false},status:'connecting',retry:vi.fn(),refetch:vi.fn()}))
vi.mock('@tanstack/react-query',()=>({useQuery:({queryKey}:any)=>queryKey[0]==='printers'?{data:state.printers,isLoading:state.loading,isError:state.error,refetch:state.refetch}:{data:state.vision}}))
vi.mock('../../hooks/useWebRTC',()=>({default:()=>({videoRef:{current:null},status:state.status,retry:state.retry})}))
function mount(){return render(<MemoryRouter initialEntries={['/cameras/9']}><Routes><Route path="/cameras/:id" element={<CameraDetail/>}/><Route path="/cameras" element={<h1>Camera index</h1>}/></Routes></MemoryRouter>)}
beforeEach(()=>{state.printers=[{id:9,name:'Synthetic printer',last_seen:new Date().toISOString(),gcode_state:'RUNNING',nozzle_temp:210.4,nozzle_target_temp:220,bed_temp:59.6,bed_target_temp:60,fan_speed:0,mc_percent:45,mc_remaining_time:75,gcode_file:'Synthetic job',layer_num:12,total_layer_num:100,filament_slots:[{color:'red',material_type:'PLA',remaining_weight:0}]}];state.error=false;state.loading=false;state.vision={enabled:false};state.status='connecting';state.retry.mockReset();state.refetch.mockReset()})
afterEach(()=>{cleanup();vi.restoreAllMocks()})
test('known printer renders real telemetry, zero fan/weight, job and layers',()=>{
 mount();expect(screen.getByRole('heading',{name:'Synthetic printer'})).toBeInTheDocument();expect(screen.getByText('210°/220°C')).toBeInTheDocument();expect(screen.getByText('60°/60°C')).toBeInTheDocument();expect(screen.getByText('0%')).toBeInTheDocument();expect(screen.getByText('0g')).toBeInTheDocument();expect(screen.getByText('Synthetic job')).toBeInTheDocument();expect(screen.getByText('45%')).toBeInTheDocument();expect(screen.getByText('1h 15m left')).toBeInTheDocument();expect(screen.getByText('Layer 12/100')).toBeInTheDocument();expect(screen.getByText('Connecting...')).toBeInTheDocument();expect(screen.queryByText('LIVE')).not.toBeInTheDocument()
})
test('paused job remains visible; idle hides job',()=>{
 state.printers[0].gcode_state='PAUSE';const view=mount();expect(screen.getByText('Current Job')).toBeInTheDocument();state.printers[0].gcode_state='IDLE';view.rerender(<MemoryRouter initialEntries={['/cameras/9']}><Routes><Route path="/cameras/:id" element={<CameraDetail/>}/></Routes></MemoryRouter>);expect(screen.queryByText('Current Job')).not.toBeInTheDocument()
})
test('unknown telemetry is omitted and absent heartbeat is offline',()=>{
 state.printers[0]={id:9,name:'Synthetic printer',gcode_state:'IDLE'};mount();expect(screen.getAllByText('Offline')).toHaveLength(2);expect(screen.queryByText('Nozzle')).not.toBeInTheDocument();expect(screen.queryByText('Bed')).not.toBeInTheDocument();expect(screen.queryByText('Fan')).not.toBeInTheDocument();expect(screen.queryByText('Filament')).not.toBeInTheDocument()
})
test('missing printer offers real return navigation',()=>{
 state.printers=[];mount();expect(screen.getByText('Printer not found')).toBeInTheDocument();fireEvent.click(screen.getByRole('link',{name:'Back to Cameras'}));expect(screen.getByRole('heading',{name:'Camera index'})).toBeInTheDocument()
})
test.each(['error','disconnected'])('stream %s provides reconnect without claiming live',status=>{
 state.status=status;mount();expect(screen.queryByText('LIVE')).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Reconnect'}));expect(state.retry).toHaveBeenCalledOnce()
})
test('enabled vision indicator is shown only from supplied enabled setting',()=>{
 state.vision={enabled:true};mount();expect(screen.getByTitle('Vigil AI active')).toBeInTheDocument()
})
test('failed printer query exposes retry rather than false not-found',()=>{
 state.printers=[];state.error=true;mount();expect(screen.getByRole('alert')).toHaveTextContent('Printer data unavailable');expect(screen.queryByText('Printer not found')).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Retry printer data'}));expect(state.refetch).toHaveBeenCalledOnce()
})

test('failed refresh hides cached telemetry and stops claiming a current stream',()=>{
 state.error=true;mount();expect(screen.getByRole('alert')).toHaveTextContent('Printer data unavailable');expect(screen.queryByText('Synthetic job')).not.toBeInTheDocument();expect(document.querySelector('video')).toBeNull()
})
test('loading data exposes status without claiming missing printer',()=>{
 state.printers=[];state.loading=true;mount();expect(screen.getByRole('status')).toHaveTextContent('Loading...');expect(screen.queryByText('Printer not found')).not.toBeInTheDocument()
})
test('fullscreen toggle requests browser fullscreen on the stream container and exits it',()=>{
 const enter=vi.fn().mockResolvedValue(undefined),exit=vi.fn().mockResolvedValue(undefined)
 Object.defineProperty(HTMLElement.prototype,'requestFullscreen',{configurable:true,value:enter})
 Object.defineProperty(document,'exitFullscreen',{configurable:true,value:exit})
 Object.defineProperty(document,'fullscreenElement',{configurable:true,value:null})
 try {
  mount();fireEvent.click(screen.getByTitle('Toggle fullscreen'));expect(enter).toHaveBeenCalledOnce();expect(enter.mock.instances[0].querySelector('video')).not.toBeNull()
  Object.defineProperty(document,'fullscreenElement',{configurable:true,value:document.querySelector('video')!.parentElement});fireEvent(document,new Event('fullscreenchange'));fireEvent.click(screen.getByTitle('Toggle fullscreen'));expect(exit).toHaveBeenCalledOnce()
 } finally { delete (HTMLElement.prototype as any).requestFullscreen;delete (document as any).exitFullscreen;delete (document as any).fullscreenElement }
})

test('rejected fullscreen request reports failure without unhandled rejection',async()=>{
 const failure=vi.spyOn(toast,'error').mockReturnValue('synthetic-toast')
 Object.defineProperty(HTMLElement.prototype,'requestFullscreen',{configurable:true,value:vi.fn().mockRejectedValue(new Error('Synthetic unavailable'))})
 try {mount();fireEvent.click(screen.getByTitle('Toggle fullscreen'));await waitFor(()=>expect(failure).toHaveBeenCalledWith('Fullscreen unavailable'))}
 finally {delete (HTMLElement.prototype as any).requestFullscreen}
})
test('rejected fullscreen exit reports failure',async()=>{
 const failure=vi.spyOn(toast,'error').mockReturnValue('synthetic-toast')
 Object.defineProperty(document,'fullscreenElement',{configurable:true,value:document.body})
 Object.defineProperty(document,'exitFullscreen',{configurable:true,value:vi.fn().mockRejectedValue(new Error('Synthetic exit unavailable'))})
 try {mount();fireEvent.click(screen.getByTitle('Toggle fullscreen'));await waitFor(()=>expect(failure).toHaveBeenCalledWith('Fullscreen unavailable'))}
 finally {delete (document as any).fullscreenElement;delete (document as any).exitFullscreen}
})
test('browser without fullscreen API reports unavailable',async()=>{
 const failure=vi.spyOn(toast,'error').mockReturnValue('synthetic-toast');mount();fireEvent.click(screen.getByTitle('Toggle fullscreen'));await waitFor(()=>expect(failure).toHaveBeenCalledWith('Fullscreen unavailable'))
})
