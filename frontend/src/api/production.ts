import { fetchAPI } from './client'
export interface RecipeOutput { product_id:number; product_name?:string; kind:'main'|'flush'; expected:number }
export interface Recipe { id:string; name:string; materials:string; qualified:boolean; reference_job_id:number; payload?:Record<string,unknown>; outputs:RecipeOutput[] }
export interface Inspection { id:string; recipe_id:string|null; job_id?:number; print_job_id?:number; kind?:string; recipe_name:string; occurred_at:string; stock_status:string; outputs:(RecipeOutput & {accepted:number;rejected:number;disposition?:'send'|'in_baseline';send_quantity?:number;baseline_version?:string|null})[] }
export interface ProductionState { recipes:Recipe[];receipts:Inspection[];additions:{id:string;receipt_id:string;state:string;attempts:number}[];square_writes_enabled:boolean;next_offset:number|null }
export interface ProductionJob {kind?:undefined;id:number;name:string;payload:{printer_id:number;plate_index:number;payload_sha256:string}}
export interface OutsideAttestation {started_outside_odin:true;file_revision_confirmed:true;statement?:string}
export interface OutsideBinding {id:string;print_job_id:number;recipe_id:string;odin_dispatched:false;owner_attested:true}
export interface OutsideProductionJob {kind:'outside_print';print_job_id:number;name:string;recipe:Recipe;payload:Record<string,unknown>}
export const production = {
  bindOutside:(printJobId:number,data:{recipe_id:string;attestation:OutsideAttestation;attested_by?:'owner_in_app'|'owner_confirmed_to_operator'})=>fetchAPI<OutsideBinding>(`/products/production/outside-prints/${printJobId}/bind`,{method:'POST',body:JSON.stringify(data)}),
  state:(offset=0)=>fetchAPI<ProductionState>(`/products/production?offset=${offset}`),
  jobs:(offset=0,jobId?:number)=>fetchAPI<{jobs:(ProductionJob|OutsideProductionJob)[];next_offset:number|null}>(`/products/production/jobs?offset=${offset}${jobId?`&job_id=${jobId}`:''}`),
  recipe:(data:{reference_job_id:number;name:string;materials:string;qualification_confirmed:boolean;outputs:RecipeOutput[]})=>fetchAPI<Recipe>('/products/production/recipes',{method:'POST',body:JSON.stringify(data)}),
  inspect:(data:{recipe_id:string;job_id?:number;print_job_id?:number;square_revision:string|null;inspection_confirmed:boolean;outputs:{product_id:number;kind:'main'|'flush';accepted:number;rejected:number;baseline_disposition?:'in_baseline'|'send'}[]})=>fetchAPI<Inspection>('/products/production/inspections',{method:'POST',body:JSON.stringify(data)}),
}

export interface StockAddition {id:string;receipt_id:string;state:'prepared'|'unknown'|'confirmed'|'withdrawn_baseline';attempts:number}
export interface InspectionHistory {receipts:Inspection[];additions:StockAddition[];next_offset:number|null;square_writes_enabled?:boolean}
export interface RecoveryCheck {id:string;state:string;attempts:number;readiness:'blocked'|'checks_passed'|'manual_review'|'confirmed'|'withdrawn_baseline';message:string;square_writes_enabled:boolean;preview?:{product:string;quantity:string;location:string;location_id:string;current_count:string|null}[];retry_allowed:false}
export const productionHistory = {
  read:(offset=0,query='',state='')=>fetchAPI<InspectionHistory>(`/products/production/history?${new URLSearchParams({offset:String(offset),query,...(state?{state}:{})})}`),
  send:(id:string)=>fetchAPI<RecoveryCheck>(`/products/batches/additions/${encodeURIComponent(id)}/send`,{method:'POST'}),
  check:(id:string)=>fetchAPI<RecoveryCheck>(`/products/production/additions/${encodeURIComponent(id)}/check`,{method:'POST'}),
}
