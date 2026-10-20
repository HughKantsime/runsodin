import {fetchAPI} from './client'
export interface ComponentLot{id:string;job_id:number;unit_description:string;accepted:number;rejected:number;available:number;source_current?:boolean;source:{model_id:number;name:string}}
export interface FinishedReceipt{id:string;recipe_name:string;outputs:{accepted:number;rejected:number;product_name:string}[];stock_status:string;occurred_at:string}
export interface FinishingState{components:ComponentLot[];finishes:FinishedReceipt[];square_writes_enabled:false}
export interface FinishPreview{bom_hash:string;bom:{product_id:number;product_name:string;components:Record<string,number>;sources?:Record<string,{name:string}>;hardware:{consumable_id:number;quantity_per_product:number}[]};components:ComponentLot[]}
const base='/products/production'
export const finishing={read:()=>fetchAPI<FinishingState>(base+'/finishing'),preview:(id:number)=>fetchAPI<FinishPreview>(`${base}/finishing/${id}/requirements`),
 inspect:(data:{job_id:number;accepted:number;rejected:number;unit_description:string;inspection_confirmed:boolean;single_component_type_confirmed:boolean})=>fetchAPI<ComponentLot>(base+'/components',{method:'POST',body:JSON.stringify(data)}),
 finish:(data:{request_id:string;product_id:number;bom_hash:string;accepted:number;rejected:number;allocations:{receipt_id:string;quantity:number}[];assembly_confirmed:boolean;hardware_confirmed:boolean;square_revision:string|null})=>fetchAPI<FinishedReceipt>(base+'/finishing',{method:'POST',body:JSON.stringify(data)})}
