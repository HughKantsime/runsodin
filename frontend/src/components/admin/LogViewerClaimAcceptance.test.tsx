import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import LogViewer from './LogViewer'
const logs = vi.hoisted(() => ({ get: vi.fn(), streamUrl: vi.fn() }))
vi.mock('../../api', () => ({ adminLogs: logs }))
class TestEventSource {
  static instances: TestEventSource[] = []
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor(public url: string, public options: unknown) { TestEventSource.instances.push(this) }
}
beforeEach(() => {
  logs.get.mockResolvedValue({ lines: ['[INFO] Synthetic connected', '[ERROR] Synthetic jam'] })
  logs.streamUrl.mockReturnValue('/api/admin/logs/stream?source=backend')
  TestEventSource.instances = []
  vi.stubGlobal('EventSource', TestEventSource)
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })
async function mount() { const view = render(<LogViewer />); await screen.findByText('[INFO] Synthetic connected'); return view }
test('F16-16.8-01/06 static fetch renders lines; source change fetches and refresh restores cleared output', async () => {
  await mount()
  expect(logs.get).toHaveBeenCalledWith('backend', 500, null, null)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'mqtt' } })
  await waitFor(() => expect(logs.get).toHaveBeenCalledWith('mqtt', 500, null, null))
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
  expect(screen.getByText('No log lines')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText('[ERROR] Synthetic jam')
})
test.each(['DEBUG', 'INFO', 'WARNING', 'ERROR'])('F16-16.8-03 %s level is sent on Refresh', async level => {
  await mount()
  fireEvent.click(screen.getByRole('button', { name: level, exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(logs.get).toHaveBeenLastCalledWith('backend', 500, level, null))
})
test('F16-16.8-04 search Enter sends keyword and renders filtered response', async () => {
  await mount()
  logs.get.mockResolvedValue({ lines: ['[ERROR] Synthetic jam'] })
  fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value: 'jam' } })
  fireEvent.keyDown(screen.getByPlaceholderText('Search...'), { key: 'Enter' })
  await waitFor(() => expect(logs.get).toHaveBeenLastCalledWith('backend', 500, null, 'jam'))
  await waitFor(() => expect(screen.queryByText('[INFO] Synthetic connected')).not.toBeInTheDocument())
})
test('F16-16.8-05 SSE appends parsed lines, ignores malformed frames and closes on stop/unmount', async () => {
  const view = await mount()
  fireEvent.click(screen.getByRole('button', { name: 'Live Stream' }))
  const stream = TestEventSource.instances[0]
  expect(stream.url).toBe('/api/admin/logs/stream?source=backend')
  expect(stream.options).toEqual({ withCredentials: true })
  act(() => { stream.onmessage?.({ data: 'bad-json' }); stream.onmessage?.({ data: JSON.stringify('[WARNING] Synthetic live') }) })
  expect(screen.getByText('[WARNING] Synthetic live')).toBeInTheDocument()
  expect(screen.queryByText('bad-json')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Stop Stream' }))
  expect(stream.close).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Live Stream' }))
  view.unmount()
  expect(TestEventSource.instances[1].close).toHaveBeenCalledOnce()
})
test('F16-16.8-05 SSE error closes connection and restores start control', async () => {
  await mount()
  fireEvent.click(screen.getByRole('button', { name: 'Live Stream' }))
  act(() => TestEventSource.instances[0].onerror?.())
  expect(screen.getByRole('button', { name: 'Live Stream' })).toBeInTheDocument()
  expect(TestEventSource.instances[0].close).toHaveBeenCalledOnce()
})
test('F16-16.8-06 failed static fetch displays failure', async () => {
  logs.get.mockRejectedValue(new Error('Synthetic fetch failure'))
  render(<LogViewer />)
  expect(await screen.findByText(/^Failed to load logs/)).toBeInTheDocument()
})
// Regression assertions retain advertised behavior and the existing Reports source.
test('F16-16.8-02 exposes all nine advertised sources including supervisord', async () => {
  await mount()
  expect(screen.getAllByRole('option').map(o => (o as HTMLOptionElement).value).sort()).toEqual(
    ['backend', 'mqtt', 'moonraker', 'prusalink', 'elegoo', 'go2rtc', 'supervisord', 'timelapse', 'vision', 'reports'].sort())
})
test('F16-16.8-04 search highlights matching keyword in returned lines', async () => {
  const view = await mount()
  fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value: 'jam' } })
  fireEvent.keyDown(screen.getByPlaceholderText('Search...'), { key: 'Enter' })
  await waitFor(() => expect(logs.get).toHaveBeenLastCalledWith('backend', 500, null, 'jam'))
  expect(view.container.querySelector('mark')?.textContent).toBe('jam')
})
test('F16-16.8-06 static pagination provides an older-page action', async () => {
  await mount()
  expect(screen.queryByRole('button', { name: /older|previous|load more|next page/i })).toBeInTheDocument()
})

test('older/newer pages use server cursor and reset to latest on Refresh',async()=>{
 logs.get.mockResolvedValueOnce({lines:['[INFO] Synthetic connected'],next_cursor:'synthetic-older'})
 await mount()
 fireEvent.click(screen.getByRole('button',{name:'Older logs'}))
 await waitFor(()=>expect(logs.get).toHaveBeenLastCalledWith('backend',500,null,null,'synthetic-older'))
 expect(await screen.findByText('Page 2')).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Newer logs'}))
 await waitFor(()=>expect(logs.get).toHaveBeenLastCalledWith('backend',500,null,null))
 expect(await screen.findByText('Page 1')).toBeInTheDocument()
})
test('literal keyword highlighting escapes HTML and regex metacharacters',async()=>{
 logs.get.mockResolvedValue({lines:['[INFO] literal <img src=x> .+ <img src=x>']})
 const view=render(<LogViewer/>);await screen.findByText('[INFO] literal <img src=x> .+ <img src=x>')
 fireEvent.change(screen.getByPlaceholderText('Search...'),{target:{value:'<img src=x>'}})
 fireEvent.keyDown(screen.getByPlaceholderText('Search...'),{key:'Enter'})
 await waitFor(()=>expect(view.container.querySelectorAll('mark')).toHaveLength(2))
 expect(view.container.querySelector('img')).toBeNull()
})
test('late static source response cannot replace newer source result',async()=>{
 let resolveOld!:(value:any)=>void
 logs.get.mockImplementationOnce(()=>new Promise(resolve=>{resolveOld=resolve})).mockResolvedValueOnce({lines:['Synthetic MQTT result']})
 render(<LogViewer/>);fireEvent.change(screen.getByRole('combobox'),{target:{value:'mqtt'}})
 await screen.findByText('Synthetic MQTT result')
 act(()=>resolveOld({lines:['Synthetic stale backend result']}))
 await waitFor(()=>expect(screen.queryByText('Synthetic stale backend result')).not.toBeInTheDocument())
})

test('returning to newer page uses its pinned snapshot cursor, not a shifted latest fetch',async()=>{
 logs.get.mockResolvedValueOnce({lines:['[INFO] Synthetic connected'],next_cursor:'older-token',page_cursor:'initial-snapshot'})
 await mount();fireEvent.click(screen.getByRole('button',{name:'Older logs'}))
 await screen.findByText('Page 2')
 fireEvent.click(screen.getByRole('button',{name:'Newer logs'}))
 await waitFor(()=>expect(logs.get).toHaveBeenLastCalledWith('backend',500,null,null,'initial-snapshot'))
})

test('actual API adapter encodes pagination cursor and filters with credentialed fetch',async()=>{
 const adapter=await import('../../api/system')
 const fetch=vi.fn().mockResolvedValue({ok:true,status:200,json:async()=>({lines:['Synthetic API line'],source:'supervisord',next_cursor:null})})
 vi.stubGlobal('fetch',fetch)
 await adapter.adminLogs.get('supervisord',500,'ERROR','literal & word','1:2:300:150')
 const url=new URL(fetch.mock.calls[0][0],'http://localhost')
 expect(url.pathname).toBe('/api/admin/logs')
 expect(Object.fromEntries(url.searchParams)).toEqual({source:'supervisord',lines:'500',level:'ERROR',search:'literal & word',cursor:'1:2:300:150'})
 expect(fetch.mock.calls[0][1].credentials).toBe('include')
})
test('failed older-page request preserves displayed page and offers refresh guidance',async()=>{
 logs.get.mockResolvedValueOnce({lines:['[INFO] Synthetic connected'],next_cursor:'older-token'}).mockRejectedValueOnce(new Error('Log file changed. Refresh to load the latest logs.'))
 await mount();fireEvent.click(screen.getByRole('button',{name:'Older logs'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('Refresh to load the latest logs')
 expect(screen.getByText('[INFO] Synthetic connected')).toBeInTheDocument()
 expect(screen.getByText('Page 1')).toBeInTheDocument()
})
test('closed stream messages and errors cannot corrupt newly selected stream',async()=>{
 await mount();fireEvent.click(screen.getByRole('button',{name:'Live Stream'}));const old=TestEventSource.instances[0]
 fireEvent.click(screen.getByRole('button',{name:'Stop Stream'}));fireEvent.click(screen.getByRole('button',{name:'Live Stream'}))
 act(()=>{old.onmessage?.({data:JSON.stringify('Synthetic stale stream')});old.onerror?.()})
 expect(screen.queryByText('Synthetic stale stream')).not.toBeInTheDocument()
 expect(screen.getByRole('button',{name:'Stop Stream'})).toBeInTheDocument()
})
