import {fetchAPI} from './client'
export interface KitItem{id:string;name:string;available:number;condition:'ready'|'damaged'|'missing';issue:string;resolution:string;active:boolean}
export interface ShowPick{item_id:string;quantity:number;name:string;ready:boolean;packed:boolean;issue:string}
export interface ShowEvent{id:string;title:string;starts_on:string;status:'planned'|'booked'|'completed'|'cancelled';picks:ShowPick[];all_selected_packed:boolean;message:string}
export interface Capacity{starts_on:string;ends_on:string;available_minutes:number;recorded_on?:string;estimates:{order_id:number;minutes:number|null;promised_by:string|null}[]}
export interface OpsState{revision:string|null;kit:KitItem[];events:ShowEvent[];capacity:Capacity|null}
export interface ConflictRow{order_id:number;order_number:string|null;platform:string|null;paid_show:boolean;show_state:string;promised_by:string|null;remaining_pieces:number;entered_minutes:number|null;needs_review:boolean}
export interface ConflictResult{revision:string|null;capacity:Capacity|null;orders:ConflictRow[];state:string;known_minutes_in_window:number;reasons:string[];truncated:boolean;message:string}
export interface Readiness{job_id:number;status:string;saved_preparation_complete:boolean;retained_physical_evidence:boolean;matching_recipes:{id:string;name:string;materials:string}[];reasons:string[];dispatch_ready:false;file_bytes_checked:false;message:string}
const base='/products/production/operations'
const put=<T>(path:string,data:unknown)=>fetchAPI<T>(base+path,{method:'PUT',body:JSON.stringify(data)})
export const operations={read:()=>fetchAPI<OpsState>(base),kit:(data:Omit<KitItem,'id'>&{revision:string|null;item_id:string|null})=>put<OpsState>('/kit',data),
 event:(data:{revision:string|null;event_id:string|null;title:string;starts_on:string;status:ShowEvent['status'];picks:{item_id:string;quantity:number}[]})=>put<OpsState>('/events',data),
 pack:(event:string,item:string,revision:string,confirmed:boolean)=>put<OpsState>(`/events/${event}/packing/${item}`,{revision,confirmed}),
 conflicts:()=>fetchAPI<ConflictResult>(base+'/conflicts'),capacity:(data:Capacity&{revision:string|null})=>put<ConflictResult>('/capacity',data),
 jobs:()=>fetchAPI<{jobs:{id:number;item_name:string;status:string}[];truncated:boolean}>(base+'/jobs'),readiness:(id:number)=>fetchAPI<Readiness>(`${base}/jobs/${id}/readiness`)}
