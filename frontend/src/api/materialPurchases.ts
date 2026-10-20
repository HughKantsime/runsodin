import {fetchAPI} from './client'
export interface Material{consumable_id:number;uid:string;name:string;unit:'piece';specification:string;current_stock:number;baseline_units:number}
export interface VendorMapping{id:string;consumable_id:number;material_uid:string;signature:{vendor:string;listing_id:string;variant:string;specification:string;units_per_pack:number}}
export interface PurchaseLine{vendor:string;order_reference:string;line_reference:string;listing_id:string;variant:string;specification:string;packs:number;units_per_pack:number;total_cost_minor?:number|null;expected_on?:string|null;source_reference?:string}
export interface IncomingLine{id:string;facts:PurchaseLine;consumable_id:number;ordered_units:number;totals:{incoming_units:number;receive_good:number;receive_damaged:number;cancel:number;return_good:number;return_damaged:number}}
export interface ReceiptInput{source_event_reference:string;request_id:string;line_id:string;kind:'receive_good'|'receive_damaged'|'cancel'|'return_good'|'return_damaged';units:number;occurred_at:string;confirmed:boolean;note:string}
export interface MaterialState{usage?:{id:number;consumable_id:number;quantity_used:number;used_at:string|null;reference:string|null}[];revision:string|null;can_register:boolean;materials:Material[];mappings:VendorMapping[];lines:IncomingLine[];events:(ReceiptInput&{id:string;stock_delta:number;recorded_at:string})[];live_import_enabled:false;message:string}
export interface CSVPreview{rows:{row:number;line:PurchaseLine|null;status:string;message:string;ordered_units?:number}[];can_import:boolean;stock_changed:false}
const base='/consumables/purchases'
const post=<T>(endpoint:string,value:unknown)=>fetchAPI<T>(base+endpoint,{method:'POST',body:JSON.stringify(value)})
export const materialPurchases={read:()=>fetchAPI<MaterialState>(base),create:(name:string,specification:string)=>post<MaterialState>('/materials',{name,specification,unit:'piece'}),
 adopt:(consumable_id:number,specification:string,counted_units:number,physically_counted:boolean)=>post<MaterialState>('/adopt',{consumable_id,specification,counted_units,physically_counted}),
 mapping:(data:VendorMapping['signature']&{consumable_id:number;verified:boolean})=>post<MaterialState>('/mappings',data),
 preview:(csv_text:string)=>post<CSVPreview>('/preview',{csv_text}),purchase:(lines:PurchaseLine[])=>post<MaterialState>('/lines',{lines}),receipt:(value:ReceiptInput)=>post<MaterialState>('/receipts',value)}
