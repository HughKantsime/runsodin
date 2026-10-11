import { afterEach, expect, test, vi } from 'vitest'
import { archives } from './archives'
afterEach(()=>vi.restoreAllMocks())
test('print-log CSV export returns actual server text and preserves filters',async()=>{
 const csv='id,print_name,filament_used_grams\r\n9,Synthetic zero,0\r\n'
 const transport=vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(csv,{status:200,headers:{'Content-Type':'text/csv; charset=utf-8'}}))
 expect(await archives.logExport({search:'Synthetic zero',printer_id:9,status:'completed',tag:'synthetic',user_id:3,start_date:'2026-10-01',end_date:'2026-10-11'})).toBe(csv)
 expect(transport).toHaveBeenCalledWith('/api/archives/log/export?printer_id=9&user_id=3&status=completed&search=Synthetic+zero&start_date=2026-10-01&end_date=2026-10-11&tag=synthetic',expect.objectContaining({credentials:'include'}))
})

test('ordinary JSON API decoding remains unchanged',async()=>{
 const {fetchAPI}=await import('./client')
 vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response('{"items":[],"total":0}',{status:200,headers:{'Content-Type':'application/json'}}))
 expect(await fetchAPI('/archives')).toEqual({items:[],total:0})
})
test('export rejects malformed successful content instead of downloading JSON text',async()=>{
 vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response('"not CSV"',{status:200,headers:{'Content-Type':'application/json'}}))
 await expect(archives.logExport()).rejects.toThrow('Unexpected export response')
})
test('export rejects unauthenticated response instead of creating undefined file contents',async()=>{
 window.history.replaceState({},'', '/login')
 try {
  vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response('{}',{status:401,headers:{'Content-Type':'application/json'}}))
  await expect(archives.logExport()).rejects.toThrow('Export unavailable. Sign in and try again.')
 } finally {window.history.replaceState({},'', '/')}
})
test.each([403,500])('export preserves HTTP %s failure instead of decoding it as CSV',async(status)=>{
 vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response('{"detail":"Synthetic export unavailable"}',{status,headers:{'Content-Type':'application/json'}}))
 await expect(archives.logExport()).rejects.toThrow('Synthetic export unavailable')
})

test('CSV export rejects successful HTML error page rather than downloading it',async()=>{
 vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response('<html>Synthetic proxy page</html>',{status:200,headers:{'Content-Type':'text/html'}}))
 await expect(archives.logExport()).rejects.toThrow('Unexpected export response')
})
test('CSV export rejects an empty 204 response rather than a null file',async()=>{
 vi.spyOn(globalThis,'fetch').mockResolvedValue(new Response(null,{status:204}))
 await expect(archives.logExport()).rejects.toThrow('Unexpected export response')
})
