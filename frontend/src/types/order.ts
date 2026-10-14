// Order domain types

export type OrderStatus =
  | 'pending'
  | 'in_progress'
  | 'partial'
  | 'fulfilled'
  | 'shipped'
  | 'cancelled';

// ---- Product ----

export interface ProductComponent {
  id: number;
  product_id: number;
  model_id: number;
  quantity_needed: number;
  notes: string | null;
  model_name: string | null;
}

export interface ProductComponentCreate {
  model_id: number;
  quantity_needed?: number;
  notes?: string;
}

export interface ProductConsumable {
  id: number;
  product_id: number;
  consumable_id: number;
  quantity_per_product: number;
  notes: string | null;
  consumable_name: string | null;
}

export interface ProductConsumableCreate {
  consumable_id: number;
  quantity_per_product?: number;
  notes?: string;
}

export interface Product {
  id: number;
  name: string;
  sku: string | null;
  price: number | null;
  description: string | null;
  created_at: string;
  updated_at: string;
  components: ProductComponent[];
  consumables: ProductConsumable[];
  estimated_cogs: number | null;
  component_count: number | null;
}

export interface ProductCreate {
  name: string;
  sku?: string;
  price?: number;
  description?: string;
  components?: ProductComponentCreate[];
}

export interface ProductUpdate {
  name?: string;
  sku?: string;
  price?: number;
  description?: string;
}

// ---- Order ----

export interface OrderItem {
  id: number;
  order_id: number;
  product_id: number;
  quantity: number;
  unit_price: number | null;
  fulfilled_quantity: number;
  production_mode?: string | null;
  can_confirm_production?: boolean;
  created_at: string;
  product_name: string | null;
  product_sku: string | null;
  subtotal: number | null;
  is_fulfilled: boolean | null;
}

export interface OrderItemCreate {
  product_id: number;
  quantity?: number;
  unit_price?: number;
}

export interface OrderItemUpdate {
  quantity?: number;
  unit_price?: number;
}

export interface Order {
  id: number;
  order_number: string | null;
  platform: string | null;
  customer_name: string | null;
  customer_email: string | null;
  order_date: string | null;
  notes: string | null;
  revenue: number | null;
  platform_fees: number | null;
  payment_fees: number | null;
  shipping_charged: number | null;
  shipping_cost: number | null;
  labor_minutes: number | null;
  status: OrderStatus;
  shipped_date: string | null;
  tracking_number: string | null;
  created_at: string;
  updated_at: string;
  items: OrderItem[];
  total_items: number | null;
  fulfilled_items: number | null;
  estimated_cost: number | null;
  actual_cost: number | null;
  profit: number | null;
  margin_percent: number | null;
  jobs_total: number | null;
  jobs_complete: number | null;
}

export interface OrderCreate {
  order_number?: string;
  platform?: string;
  customer_name?: string;
  customer_email?: string;
  order_date?: string;
  notes?: string;
  revenue?: number;
  platform_fees?: number;
  payment_fees?: number;
  shipping_charged?: number;
  shipping_cost?: number;
  labor_minutes?: number;
  items?: OrderItemCreate[];
}

export interface OrderUpdate {
  order_number?: string;
  platform?: string;
  customer_name?: string;
  customer_email?: string;
  status?: OrderStatus;
  order_date?: string;
  shipped_date?: string;
  tracking_number?: string;
  notes?: string;
  revenue?: number;
  platform_fees?: number;
  payment_fees?: number;
  shipping_charged?: number;
  shipping_cost?: number;
  labor_minutes?: number;
}

export interface OrderShipRequest {
  tracking_number?: string;
  shipped_date?: string;
}

// ---- Pricing Config ----

export interface PricingConfig {
  spool_cost?: number;
  spool_weight?: number;
  hourly_rate?: number;
  electricity_rate?: number;
  printer_wattage?: number;
  printer_cost?: number;
  printer_lifespan?: number;
  packaging_cost?: number;
  failure_rate?: number;
  monthly_rent?: number;
  parts_per_month?: number;
  post_processing_min?: number;
  packing_min?: number;
  support_min?: number;
  default_margin?: number;
  other_costs?: number;
  ui_mode?: string;
}

// ---- Chargebacks ----

export interface ChargebackRow {
  user_id: number;
  username: string;
  job_count: number;
  total_hours: number | null;
  total_grams?: number | null;
  total_cost: number | null;
  currency?: string | null;
}
export type ChargebackReport = ChargebackRow[];
