import {fetchAPI} from './client'
export type AssortmentMode='active'|'trial'|'inactive'
export interface AssortmentNumbers {reorder_point:number;target:number;minimum_batch:number}
/** `kind` is only present on a policy saved before the numbers existed; it is read, never sent. */
export interface AssortmentPolicy extends AssortmentNumbers {assortment:AssortmentMode;kind?:string|null}
export type AssortmentEdit=AssortmentNumbers&{assortment:AssortmentMode;revision:string|null}
export interface AssortmentProduct {id:number;name:string;policy:AssortmentPolicy|null;rules:{trigger_below:number|null;minimum_main_batch:number|null;routine_replenishment:boolean;reorder_point?:number|null;target?:number|null;minimum_batch?:number|null}}
export interface AssortmentState {revision:string|null;products:AssortmentProduct[];truncated:boolean}
export interface ReplenishmentPreview {product_id:number;status:string;message:string;stock:{quantity:string|null;state:string;calculated_at:string|null}|null;retrieved_at:string|null;advisory_only:true;recipes:{id:string;name:string;main_yield:number;printer_id:number;materials:string;outputs:{product_name:string;kind:string;expected:number}[]}[]}
export const assortment={
 read:()=>fetchAPI<AssortmentState>('/products/production/assortment'),
 save:(id:number,data:AssortmentEdit)=>fetchAPI<AssortmentState>(`/products/production/assortment/${id}`,{method:'PUT',body:JSON.stringify(data)}),
 preview:(id:number)=>fetchAPI<ReplenishmentPreview>(`/products/production/assortment/${id}/preview`,{method:'POST'}),
}
