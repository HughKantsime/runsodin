import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import { QueryClient,QueryClientProvider } from '@tanstack/react-query'
import { afterEach,beforeEach,expect,test,vi } from 'vitest'
import ProjectsPage from './Projects'
const state=vi.hoisted(()=>({role:'operator',importZip:vi.fn(),exportZip:vi.fn()}))
vi.mock('../../api',()=>({projects:{
 list:vi.fn(async()=>[{id:101,name:'Synthetic project',status:'active',color:'#112233',archive_count:1}]),
 get:vi.fn(async()=>({id:101,name:'Synthetic project',status:'active',archive_count:1,archives:[]})),
 create:vi.fn(),update:vi.fn(),delete:vi.fn(),
 importZip:(...args:any[])=>state.importZip(...args),exportZip:(...args:any[])=>state.exportZip(...args)}}))
vi.mock('../../permissions',()=>({canDo:()=>state.role!=='viewer'}))
vi.mock('react-hot-toast',()=>({default:{success:vi.fn(),error:vi.fn()}}))
import toast from 'react-hot-toast'
function mount(){return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})}><ProjectsPage/></QueryClientProvider>)}
beforeEach(()=>{vi.clearAllMocks();state.role='operator'})
afterEach(cleanup)

test('import uploads the chosen ZIP and reports restored archive count',async()=>{
 state.importZip.mockResolvedValue({id:7,name:'Restored',status:'imported',archives:2,files:2})
 mount();await screen.findByText('Synthetic project')
 const file=new File(['zip'],'p.zip',{type:'application/zip'})
 fireEvent.change(screen.getByLabelText('Import project ZIP'),{target:{files:[file]}})
 await waitFor(()=>expect(state.importZip).toHaveBeenCalledWith(file))
 await waitFor(()=>expect(toast.success).toHaveBeenCalledWith('Imported "Restored" with 2 archives'))
})

test('refused import surfaces the server reason and never announces success',async()=>{
 state.importZip.mockRejectedValue(new Error('Invalid, unsupported or oversized project ZIP'))
 mount();await screen.findByText('Synthetic project')
 fireEvent.change(screen.getByLabelText('Import project ZIP'),{target:{files:[new File(['x'],'p.zip')]}})
 await waitFor(()=>expect(toast.error).toHaveBeenCalledWith('Invalid, unsupported or oversized project ZIP'))
 expect(toast.success).not.toHaveBeenCalled()
})

test('export downloads the selected project',async()=>{
 state.exportZip.mockResolvedValue({count:0,names:[]})
 mount();fireEvent.click(await screen.findByText('Synthetic project'))
 fireEvent.click(await screen.findByRole('button',{name:/Export ZIP/}))
 await waitFor(()=>expect(state.exportZip).toHaveBeenCalledWith(101,'Synthetic project'))
 await waitFor(()=>expect(toast.success).toHaveBeenCalledWith('Project exported'))
})

test('export lists oversized files left out of the archive',async()=>{
 state.exportZip.mockResolvedValue({
  count:17,names:['giant-plate.3mf'],
 })
 mount();fireEvent.click(await screen.findByText('Synthetic project'))
 fireEvent.click(await screen.findByRole('button',{name:/Export ZIP/}))
 await screen.findByText('17 files were left out because they are larger than 128 MiB.')
 expect(screen.getByText('giant-plate.3mf')).toBeInTheDocument()
 expect(screen.getByText('The full list is in project.json inside the ZIP.')).toBeInTheDocument()
 let fail!: (reason: Error) => void
 state.exportZip.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
 fireEvent.click(screen.getByRole('button',{name:/Export ZIP/}))
 await waitFor(()=>expect(screen.queryByText('giant-plate.3mf')).not.toBeInTheDocument())
 fail(new Error('Export failed'))
 await waitFor(()=>expect(toast.error).toHaveBeenCalledWith('Export failed'))
 expect(screen.queryByText('giant-plate.3mf')).not.toBeInTheDocument()
})

test('viewer has no import or export controls',async()=>{
 state.role='viewer';mount();fireEvent.click(await screen.findByText('Synthetic project'))
 await screen.findByText('Linked Archives (0)')
 expect(screen.queryByLabelText('Import project ZIP')).not.toBeInTheDocument()
 expect(screen.queryByRole('button',{name:/Export ZIP/})).not.toBeInTheDocument()
})
