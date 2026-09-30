import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import PrinterCard from './PrinterCard'
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../api', () => ({ printers: { assignSlotSpool: vi.fn(), toggleLights: vi.fn(), setSpeed: vi.fn() } }))
afterEach(cleanup)
const localSlot={id:1,slot_number:1,color_hex:'123456',material_type:'PLA',color:'Test blue',remaining:45,assigned_spool_id:null,spool_confirmed:false}
const base:any={id:42,name:'Test U1',nickname:null,model:'Test printer',is_active:true,api_type:'moonraker',api_host:null,has_api_key:false,camera_url:null,bed_temp:null,bed_target_temp:null,nozzle_temp:null,nozzle_target_temp:null,gcode_state:null,print_stage:null,lights_on:null,tags:[],machine_type:'U1',filament_slots:[localSlot]}
function setup(overrides:any={}) {
 const onUpdateSlot=vi.fn(),onScanSpool=vi.fn(),onSyncAms=vi.fn()
 const rendered=render(<PrinterCard printer={{...base,...overrides}} allFilaments={[]} spools={[]} onDelete={vi.fn()} onToggleActive={vi.fn()} onUpdateSlot={onUpdateSlot} onEdit={vi.fn()} onSyncAms={onSyncAms} onScanSpool={onScanSpool}/>)
 return {...rendered,onUpdateSlot,onScanSpool,onSyncAms}
}
it('external mapped slots show Ledger material/color/remaining and never allow local edits',()=>{
 const {onUpdateSlot,onScanSpool,onSyncAms}=setup({api_type:'bambu',api_host:'printer.invalid',has_api_key:true,filament_source:'filament-ledger',filament_source_status:'fresh',filament_slots:[{...localSlot,display_name:'Tool 1',mapping_status:'mapped',material_type:'PLA+',external_spool_id:765}]})
 expect(screen.getByText('Filament Ledger · read only')).toBeInTheDocument()
 const slot=screen.getByLabelText('Tool 1');expect(within(slot).getByText('PLA+')).toBeInTheDocument();expect(within(slot).getByText('Test blue')).toBeInTheDocument();expect(within(slot).getByText('Mapped spool #765')).toBeInTheDocument();expect(within(slot).getByText('45% remaining')).toBeInTheDocument();expect(slot.querySelector('svg circle:nth-child(2)')).toHaveAttribute('stroke','#123456')
 fireEvent.click(slot)
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(screen.queryByLabelText('Scan spool QR code')).not.toBeInTheDocument();expect(screen.queryByTitle('Slots need spool assignment')).not.toBeInTheDocument();expect(screen.queryByText('(click to edit)')).not.toBeInTheDocument();expect(screen.queryByTitle('Sync filament state from printer')).not.toBeInTheDocument()
 expect(onUpdateSlot).not.toHaveBeenCalled();expect(onScanSpool).not.toHaveBeenCalled();expect(onSyncAms).not.toHaveBeenCalled()
})
it('partial mappings distinguish unknown mapping from an empty spool',()=>{
 setup({filament_source:'filament-ledger',filament_source_status:'partial',filament_slots:[{...localSlot,display_name:'Tool 2',mapping_status:'unmapped',color_hex:null,material_type:null}]})
 expect(screen.getByRole('status')).toHaveTextContent('Some slots could not be matched or read from Filament Ledger.')
 expect(within(screen.getByLabelText('Tool 2')).getByText('Unmapped')).toBeInTheDocument();expect(screen.queryByText('Empty')).not.toBeInTheDocument()
})
it('Ledger outage reports unavailable and offers no local assignment tools',()=>{
 setup({filament_source:'filament-ledger',filament_source_status:'unavailable',filament_slots:[{...localSlot,display_name:'Tool 3',mapping_status:'unavailable',color_hex:null,material_type:null,remaining:null}]})
 expect(screen.getByRole('status')).toHaveTextContent('Filament Ledger is unavailable.')
 expect(within(screen.getByLabelText('Tool 3')).getByText('Spool information unavailable')).toBeInTheDocument();expect(screen.queryByText('Empty')).not.toBeInTheDocument();expect(screen.queryByLabelText('Scan spool QR code')).not.toBeInTheDocument()
})
it('unknown remaining amount does not render a full spool estimate',()=>{
 setup({filament_source:'filament-ledger',filament_source_status:'fresh',filament_slots:[{...localSlot,display_name:'Tool 1',mapping_status:'mapped',remaining:null}]})
 const slot=screen.getByLabelText('Tool 1');expect(within(slot).getByText('Remaining amount unknown')).toBeInTheDocument();expect(slot.querySelector('svg')).toBeNull();expect(within(slot).getByLabelText('Spool color')).toHaveStyle({backgroundColor:'#123456'})
})
it('ordinary local printer retains editable slot and QR scanning',()=>{
 setup()
 expect(screen.getByText('Loaded Filaments')).toBeInTheDocument();expect(screen.getByLabelText('Scan spool QR code')).toBeInTheDocument();expect(screen.getByTitle('Slots need spool assignment')).toBeInTheDocument()
 const color=screen.getByText('Test blue');fireEvent.click(color.parentElement!)
 expect(screen.getByRole('dialog',{name:'Select filament for slot 1'})).toBeInTheDocument()
})
