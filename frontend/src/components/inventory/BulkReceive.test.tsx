import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import BulkReceive from './BulkReceive'
import { spools, filaments } from '../../api/inventory'
import { csvExample, previewCsv } from './receivingCsv'
vi.mock('../../api/inventory', () => ({ spools: { receiveBulk: vi.fn() }, filaments: { list: vi.fn() } }))
beforeEach(() => { vi.clearAllMocks(); vi.mocked(filaments.list).mockResolvedValue([]); vi.mocked(spools.receiveBulk).mockResolvedValue({ import_id:'test',spools:[{id:1,qr_code:'SPL-1'}] }) })
function mount() {
  const received = vi.fn(), close = vi.fn()
  render(<QueryClientProvider client={new QueryClient({ defaultOptions:{queries:{retry:false}} })}><BulkReceive onClose={close} onReceived={received}/></QueryClientProvider>)
  return {received,close}
}
it('previews new lines and every receipt fact without writing, then confirms', async () => {
  const {received} = mount()
  fireEvent.change(screen.getByLabelText('CSV rows'), {target:{value:csvExample}})
  await waitFor(()=>expect(screen.getByText('Preview rows')).not.toBeDisabled())
  fireEvent.click(screen.getByText('Preview rows'))
  expect(screen.getByText('New product line')).toBeInTheDocument()
  expect(screen.getByText('Nardo Gray')).toBeInTheDocument()
  expect(spools.receiveBulk).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('Confirm receipt'))
  await waitFor(()=>expect(received).toHaveBeenCalledWith([1]))
  expect(vi.mocked(spools.receiveBulk).mock.calls[0][0].rows[0]).toMatchObject({brand:'Bambu Lab',product_line:'PLA Matte',material:'PLA',color_name:'Nardo Gray',quantity:1,sealed:true})
})
it('blocks unknown materials and invalid rows; editing invalidates preview', async () => {
  mount()
  fireEvent.change(screen.getByLabelText('CSV rows'),{target:{value:csvExample.replace(',PLA,Nardo',',mystery,Nardo')}})
  await waitFor(()=>expect(screen.getByText('Preview rows')).not.toBeDisabled())
  fireEvent.click(screen.getByText('Preview rows'))
  expect(screen.getByRole('alert')).toHaveTextContent('Unknown material')
  expect(screen.getByText('Confirm receipt')).toBeDisabled()
  fireEvent.change(screen.getByLabelText('CSV rows'),{target:{value:csvExample}})
  expect(screen.queryByText('Confirm receipt')).not.toBeInTheDocument()
  expect(spools.receiveBulk).not.toHaveBeenCalled()
})
it('retains the same import id and rows when retrying an uncertain receipt', async () => {
  vi.mocked(spools.receiveBulk).mockRejectedValueOnce(new Error('Connection lost'))
  mount()
  fireEvent.change(screen.getByLabelText('CSV rows'),{target:{value:csvExample}})
  await waitFor(()=>expect(screen.getByText('Preview rows')).not.toBeDisabled())
  fireEvent.click(screen.getByText('Preview rows'));fireEvent.click(screen.getByText('Confirm receipt'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Connection lost')
  fireEvent.click(screen.getByText('Confirm receipt'))
  await waitFor(()=>expect(spools.receiveBulk).toHaveBeenCalledTimes(2))
  expect(vi.mocked(spools.receiveBulk).mock.calls[0][0]).toEqual(vi.mocked(spools.receiveBulk).mock.calls[1][0])
})
it('reads an uploaded CSV and does not receive before confirmation', async () => {
  mount()
  fireEvent.change(screen.getByLabelText('CSV file'), {target:{files:[{size:100,text:async()=>csvExample}]}})
  await waitFor(()=>expect(screen.getByLabelText('CSV rows')).toHaveValue(csvExample))
  expect(spools.receiveBulk).not.toHaveBeenCalled()
})
it('supports quoted commas, CRLF, escaped quotes and explicit open rolls', () => {
  const source='brand,product_line,material,color_name,quantity,storage_location,sealed,note\r\nBambu,PLA Silk,PLA,"Beach, blue",2,Shelf,false,"A ""quoted"" note"'
  const [preview]=previewCsv(source,[{brand:'Bambu',name:'PLA Silk',material:'PLA'}])
  expect(preview.errors).toEqual([]);expect(preview.newProduct).toBe(false)
  expect(preview.row).toMatchObject({color_name:'Beach, blue',note:'A "quoted" note',sealed:false})
  expect(()=>previewCsv('brand,brand\nA,A',[])).toThrow('duplicate')
})
