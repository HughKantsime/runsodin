import {fetchAPI} from './client'
export type AssortmentMode='active'|'trial'|'inactive'
export type ProductKind='multicolor_character'|'dragon_jumbo'|'dragon_half'|'dragon_other'|'other'
export interface AssortmentPolicy {assortment:AssortmentMode;kind:ProductKind}
export interface AssortmentProduct {id:number;name:string;policy:AssortmentPolicy|null;rules:{trigger_below:number|null;minimum_main_batch:number|null;routine_replenishment:boolean}}
export interface AssortmentState {revision:string|null;products:AssortmentProduct[];truncated:boolean}
export interface ReplenishmentPreview {product_id:number;status:string;message:string;stock:{quantity:string|null;state:string;calculated_at:string|null}|null;retrieved_at:string|null;advisory_only:true;recipes:{id:string;name:string;main_yield:number;printer_id:number;materials:string;outputs:{product_name:string;kind:string;expected:number}[]}[]}
export const assortment={
 read:()=>fetchAPI<AssortmentState>('/products/production/assortment'),
 save:(id:number,data:AssortmentPolicy&{revision:string|null})=>fetchAPI<AssortmentState>(`/products/production/assortment/${id}`,{method:'PUT',body:JSON.stringify(data)}),
 preview:(id:number)=>fetchAPI<ReplenishmentPreview>(`/products/production/assortment/${id}/preview`,{method:'POST'}),
}
