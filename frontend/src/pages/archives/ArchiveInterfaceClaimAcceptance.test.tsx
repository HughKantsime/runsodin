import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Archives from './Archives'
import PrintLog from '../analytics/PrintLog'
const api=vi.hoisted(()=>({list:vi.fn(),update:vi.fn(),compare:vi.fn(),log:vi.fn(),edit:true,remove:true}))
vi.mock('../../api',()=>({archives:{list:api.list,update:api.update,compare:api.compare,log:api.log},printers:{list:vi.fn().mockResolvedValue([{id:9,name:'Synthetic printer'}])}}))
vi.mock('../../permissions',()=>({canDo:(action:string)=>action==='printers.edit'?api.edit:action==='printers.delete'?api.remove:false}))
const a={id:11,print_name:'Synthetic A',printer_display:'Synthetic printer',status:'completed',actual_duration_seconds:3600,filament_used_grams:25,cost_estimate:2,started_at:'2026-10-12T12:00:00Z',completed_at:'2026-10-12T13:00:00Z',thumbnail_b64:'synthetic',tags:['Synthetic tag'],notes:'Synthetic saved note'}
const b={...a,id:12,print_name:'Synthetic B',status:'failed',filament_used_grams:40,cost_estimate:3}
beforeEach(()=>{api.edit=true;api.remove=true;api.list.mockResolvedValue({items:[a,b],total:101});api.log.mockResolvedValue({items:[a,b],total:101});api.compare.mockResolvedValue({a,b,diff:{status:true,filament_used_grams:true,cost_estimate:true}});api.update.mockResolvedValue({})})
afterEach(()=>{cleanup();vi.clearAllMocks()})
function mount(page=<Archives/>,path='/archives'){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{page}</MemoryRouter></QueryClientProvider>)}
async function loaded(){const view=mount();await screen.findByText('Synthetic A');return view}
test('F02-2.8-11 archive table shows thumbnail/name/printer/outcome/duration/filament/date',async()=>{
 await loaded();const row=screen.getByText('Synthetic A').closest('tr')!
 expect(row.querySelector('img')).toHaveAttribute('src','data:image/png;base64,synthetic')
 expect(within(row).getByText('Synthetic printer')).toBeInTheDocument();expect(within(row).getByText('completed')).toBeInTheDocument();expect(within(row).getByText('25.0g')).toBeInTheDocument()
 expect(row.textContent).toContain('1h');expect(row.textContent).toMatch(/2026|Oct|10\/12/)
})
test('F02-2.8-12 text/printer/status filters send combined parameters',async()=>{
 await loaded();await screen.findByRole('option',{name:'Synthetic printer'})
 fireEvent.change(screen.getByPlaceholderText('Search prints...'),{target:{value:'Synthetic A'}})
 fireEvent.keyDown(screen.getByPlaceholderText('Search prints...'),{key:'Enter'})
 const selects=screen.getAllByRole('combobox');fireEvent.change(selects[0],{target:{value:'9'}});fireEvent.change(selects[1],{target:{value:'failed'}})
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({search:'Synthetic A',printer_id:'9',status:'failed',page:1,per_page:50})))
})
test('F02-2.8-13 fifty-item pagination sends next page and disables previous initially',async()=>{
 await loaded();const controls=screen.getByText('1 / 3').parentElement!
 const buttons=within(controls).getAllByRole('button');expect(buttons[0]).toBeDisabled();fireEvent.click(buttons[1])
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({page:2,per_page:50})))
 expect(await screen.findByText('2 / 3')).toBeInTheDocument()
})
test('F02-2.8-14 detail renders metadata and notes save submits exact archive',async()=>{
 await loaded();fireEvent.click(screen.getByText('Synthetic A'))
 const dialog=screen.getByRole('dialog');expect(within(dialog).getByText('25.0g')).toBeInTheDocument();expect(within(dialog).getByRole('img')).toBeInTheDocument()
 fireEvent.change(within(dialog).getByPlaceholderText('Add notes...'),{target:{value:'Synthetic changed note'}})
 fireEvent.click(within(dialog).getByRole('button',{name:'Save Notes'}))
 await waitFor(()=>expect(api.update).toHaveBeenCalledExactlyOnceWith(11,{notes:'Synthetic changed note'}))
 expect(within(dialog).getByRole('button',{name:'Delete Archive'})).toBeInTheDocument()
})
test('F02-2.8-14 denied edit/delete permissions remove mutation controls',async()=>{
 api.edit=false;api.remove=false;await loaded();fireEvent.click(screen.getByText('Synthetic A'));const dialog=screen.getByRole('dialog')
 expect(within(dialog).queryByPlaceholderText('Add notes...')).not.toBeInTheDocument();expect(within(dialog).queryByRole('button',{name:'Delete Archive'})).not.toBeInTheDocument();expect(api.update).not.toHaveBeenCalled()
})
test('F02-2.8-19/20 two selected archives compare exact IDs and highlight differing rows',async()=>{
 await loaded();screen.getAllByRole('checkbox').forEach(box=>fireEvent.click(box));fireEvent.click(screen.getByRole('button',{name:'Compare'}))
 const dialog=screen.getByRole('dialog');await within(dialog).findByText('$2.00')
 expect(api.compare).toHaveBeenCalledWith(11,12);expect(within(dialog).getByRole('columnheader',{name:'Synthetic A'})).toBeInTheDocument();expect(within(dialog).getByRole('columnheader',{name:'Synthetic B'})).toBeInTheDocument()
 expect(within(dialog).getByText('Cost').closest('tr')).toHaveClass('bg-amber-900/20');expect(within(dialog).getByText('Printer').closest('tr')).not.toHaveClass('bg-amber-900/20')
 expect(within(dialog).getByText('Filament (g)')).toBeInTheDocument();expect(within(dialog).getByText('Started')).toBeInTheDocument();expect(within(dialog).getByText('Completed')).toBeInTheDocument()
})
test('F02-2.8-23 clicking tag forwards tag filter and resets page',async()=>{
 await loaded();fireEvent.click(screen.getAllByRole('button',{name:'Synthetic tag'})[0])
 await waitFor(()=>expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({tag:'Synthetic tag',page:1})))
})
test('F02-2.8-16 PrintLog displays chronological API rows and URL filters/page request',async()=>{
 mount(<PrintLog/>,'/print-log?q=Synthetic&printer=9&status=completed&page=2')
 await screen.findByText('Synthetic A');expect(screen.getByRole('table')).toBeInTheDocument()
 expect(api.log).toHaveBeenCalledWith(expect.objectContaining({page:2,per_page:50,search:'Synthetic',printer_id:'9',status:'completed'}))
 const names=screen.getAllByText(/^Synthetic [AB]$/).map(e=>e.textContent);expect(names).toEqual(['Synthetic A','Synthetic B'])
})
