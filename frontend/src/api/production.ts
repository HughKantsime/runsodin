import { fetchAPI } from './client'
export interface RecipeOutput { product_id:number; product_name?:string; kind:'main'|'flush'; expected:number }
export interface Recipe { id:string; name:string; materials:string; qualified:boolean; reference_job_id:number; outputs:RecipeOutput[] }
export interface Inspection { id:string; recipe_id:string; job_id:number; recipe_name:string; occurred_at:string; stock_status:string; outputs:(RecipeOutput & {accepted:number;rejected:number})[] }
export interface ProductionState { recipes:Recipe[];receipts:Inspection[];additions:{id:string;receipt_id:string;state:string;attempts:number}[];square_writes_enabled:boolean;next_offset:number|null }
export interface ProductionJob {id:number;name:string;payload:{printer_id:number;plate_index:number;payload_sha256:string}}
export const production = {
  state:(offset=0)=>fetchAPI<ProductionState>(`/products/production?offset=${offset}`),
  jobs:(offset=0)=>fetchAPI<{jobs:ProductionJob[];next_offset:number|null}>(`/products/production/jobs?offset=${offset}`),
  recipe:(data:{reference_job_id:number;name:string;materials:string;qualification_confirmed:boolean;outputs:RecipeOutput[]})=>fetchAPI<Recipe>('/products/production/recipes',{method:'POST',body:JSON.stringify(data)}),
  inspect:(data:{recipe_id:string;job_id:number;square_revision:string|null;inspection_confirmed:boolean;outputs:{product_id:number;kind:'main'|'flush';accepted:number;rejected:number}[]})=>fetchAPI<Inspection>('/products/production/inspections',{method:'POST',body:JSON.stringify(data)}),
}

export interface StockAddition {id:string;receipt_id:string;state:'prepared'|'unknown'|'confirmed';attempts:number}
export interface InspectionHistory {receipts:Inspection[];additions:StockAddition[];next_offset:number|null}
export interface RecoveryCheck {id:string;state:string;attempts:number;readiness:'blocked'|'checks_passed'|'manual_review'|'confirmed';message:string;square_writes_enabled:false;retry_allowed:false}
export const productionHistory = {
  read:(offset=0,query='',state='')=>fetchAPI<InspectionHistory>(`/products/production/history?${new URLSearchParams({offset:String(offset),query,...(state?{state}:{})})}`),
  check:(id:string)=>fetchAPI<RecoveryCheck>(`/products/production/additions/${encodeURIComponent(id)}/check`,{method:'POST'}),
}
