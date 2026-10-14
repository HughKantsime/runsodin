import {afterEach,expect,test,vi} from 'vitest'
import {alerts} from './notifications'
vi.mock('../permissions',()=>({clearSensitiveBrowserState:vi.fn(async()=>{})}))
afterEach(()=>vi.unstubAllGlobals())
test('actual client 401 cannot produce a confirmed deletion',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>({status:401,ok:false})))
 await expect(alerts.remove(701)).rejects.toThrow('Deletion was not confirmed')
})
test('only exact server success receipt confirms deletion',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>({status:200,ok:true,json:async()=>({status:'ok',alert_id:702})})))
 await expect(alerts.remove(701)).rejects.toThrow('Deletion was not confirmed')
 vi.stubGlobal('fetch',vi.fn(async()=>({status:200,ok:true,json:async()=>({status:'ok',alert_id:701})})))
 await expect(alerts.remove(701)).resolves.toEqual({status:'ok',alert_id:701})
})
