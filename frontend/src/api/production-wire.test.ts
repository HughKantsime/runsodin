import { afterEach, expect, test, vi } from 'vitest'
import { production } from './production'
import fixtures from '../../../tests/accessibility/fixtures/common.json'

afterEach(()=>vi.unstubAllGlobals())
test('qualification and binding clients use declared Education journey wire fixtures',async()=>{
 const data={printer_id:301,print_file_id:501,plate_index:0,name:'Latch',materials:'PLA',qualification_confirmed:true as const,outputs:[{product_id:101,kind:'main' as const,expected:150}],attestation:{started_outside_odin:true as const,file_revision_confirmed:true as const}}
 const transport=vi.fn().mockImplementation(async(url:string)=>new Response(JSON.stringify(fixtures[`POST ${url}` as keyof typeof fixtures]),{status:200,headers:{'Content-Type':'application/json'}}))
 vi.stubGlobal('fetch',transport)
 expect(await production.qualifyOutside(120,data)).toEqual(fixtures['POST /api/products/production/outside-prints/120/qualify-and-bind'])
 expect(transport).toHaveBeenLastCalledWith('/api/products/production/outside-prints/120/qualify-and-bind',expect.objectContaining({credentials:'include',method:'POST',body:JSON.stringify(data)}))
 const binding={recipe_id:'a'.repeat(32),attestation:data.attestation}
 expect(await production.bindOutside(120,binding)).toEqual(fixtures['POST /api/products/production/outside-prints/120/bind'])
 expect(transport).toHaveBeenLastCalledWith('/api/products/production/outside-prints/120/bind',expect.objectContaining({credentials:'include',method:'POST',body:JSON.stringify(binding)}))
})
