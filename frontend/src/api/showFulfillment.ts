import {fetchAPI} from './client'
export interface ShippingAddress {recipient:string;line1:string;line2:string;city:string;region:string;postal_code:string;country:'US'}
export interface ShowIntake {revision:string|null;correction_note?:string;address:ShippingAddress;address_confirmed:boolean;source:string;dispatch_by:string;dispatch_timezone?:string;currency:'USD';amount_received_minor:number;payment_method:'square_external'|'cash'|'other_external';payment_reference:string;payment_received_at:string|null;payment_confirmed:boolean}
export interface PackageQuote {length_mm:number;width_mm:number;height_mm:number;weight_g:number;carrier:string;service:string;source:string;amount_minor:number;quoted_at:string;valid_until:string;reviewed:boolean}
export interface ShowFacts {shipping?:{state:string;quote:PackageQuote|null;carrier_verified:false};enrolled:boolean;order_id:number;order_status:string;production_remaining:number;can_dispatch:boolean;state:string;needs_review:boolean;revision:string|null;quoted_total_minor?:number;details:Omit<ShowIntake,'revision'>|null;dispatch?:{carrier:string;tracking_number:string;handed_at:string}|null;payment_history?:{amount_received_minor:number;payment_method:string;payment_reference:string;recorded_at:string}[];overdue?:boolean;payment_verification:string}
export interface ShowCommitments {orders:{order_id:number;order_number:string|null;state:string;dispatch_by:string;production_remaining:number;overdue:boolean;priority:string}[];next_offset:number|null;capacity_verified:false;message:string}
const path=(id:number)=>`/orders/${id}/show-fulfillment`
export const showFulfillment={
 read:(id:number)=>fetchAPI<ShowFacts>(path(id)),
 save:(id:number,data:ShowIntake)=>fetchAPI<ShowFacts>(path(id),{method:'PUT',body:JSON.stringify(data)}),
 dispatch:(id:number,data:{revision:string;carrier:string;tracking_number:string;handed_at:string;physically_handed_over:boolean})=>fetchAPI<ShowFacts>(path(id)+'/dispatch',{method:'POST',body:JSON.stringify(data)}),
 shipping:(id:number,revision:string,quote:PackageQuote)=>fetchAPI<ShowFacts>(path(id)+'/shipping',{method:'PUT',body:JSON.stringify({revision,quote})}),
 commitments:(offset=0)=>fetchAPI<ShowCommitments>(`/orders/show-commitments?offset=${offset}`),
}
