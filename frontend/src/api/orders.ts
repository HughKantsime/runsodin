import { fetchAPI } from './client'
import type {
  Product,
  ProductCreate,
  ProductUpdate,
  ProductComponentCreate,
  ProductConsumableCreate,
  Order,
  OrderCreate,
  OrderUpdate,
  OrderItemCreate,
  OrderItemUpdate,
  OrderShipRequest,
  OrderStatus,
  PricingConfig,
  ChargebackReport,
} from '../types'

const API_BASE = '/api'

// Products (v0.14.0)
export const products = {
  list: (): Promise<Product[]> => fetchAPI('/products'),
  get: (id: number): Promise<Product> => fetchAPI(`/products/${id}`),
  create: (data: ProductCreate): Promise<Product> => fetchAPI('/products', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: ProductUpdate): Promise<Product> => fetchAPI(`/products/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/products/${id}`, { method: 'DELETE' }),
  addComponent: (productId: number, data: ProductComponentCreate): Promise<unknown> => fetchAPI(`/products/${productId}/components`, { method: 'POST', body: JSON.stringify(data) }),
  removeComponent: (productId: number, componentId: number): Promise<void> => fetchAPI(`/products/${productId}/components/${componentId}`, { method: 'DELETE' }),
  addConsumable: (productId: number, data: ProductConsumableCreate): Promise<unknown> => fetchAPI(`/products/${productId}/consumables`, { method: 'POST', body: JSON.stringify(data) }),
  removeConsumable: (productId: number, linkId: number): Promise<void> => fetchAPI(`/products/${productId}/consumables/${linkId}`, { method: 'DELETE' }),
}

// Orders (v0.14.0)
export const orders = {
  list: (status?: OrderStatus, platform?: string): Promise<Order[]> => {
    const params = new URLSearchParams()
    if (status) params.append('status_filter', status)
    if (platform) params.append('platform', platform)
    const query = params.toString()
    return fetchAPI('/orders' + (query ? '?' + query : ''))
  },
  get: (id: number): Promise<Order> => fetchAPI(`/orders/${id}`),
  create: (data: OrderCreate): Promise<Order> => fetchAPI('/orders', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: OrderUpdate): Promise<Order> => fetchAPI(`/orders/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/orders/${id}`, { method: 'DELETE' }),
  addItem: (orderId: number, data: OrderItemCreate): Promise<unknown> => fetchAPI(`/orders/${orderId}/items`, { method: 'POST', body: JSON.stringify(data) }),
  updateItem: (orderId: number, itemId: number, data: OrderItemUpdate): Promise<unknown> => fetchAPI(`/orders/${orderId}/items/${itemId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  removeItem: (orderId: number, itemId: number): Promise<void> => fetchAPI(`/orders/${orderId}/items/${itemId}`, { method: 'DELETE' }),
  schedule: (id: number): Promise<unknown> => fetchAPI(`/orders/${id}/schedule`, { method: 'POST' }),
  ship: (id: number, data: OrderShipRequest): Promise<Order> => fetchAPI(`/orders/${id}/ship`, { method: 'PATCH', body: JSON.stringify(data) }),
}

// Pricing Config
export const pricingConfig = {
  get: (): Promise<PricingConfig> => fetchAPI('/pricing-config'),
  update: (data: Partial<PricingConfig>): Promise<PricingConfig> => fetchAPI('/pricing-config', { method: 'PUT', body: JSON.stringify(data) }),
}

// Chargebacks
export const chargebacks = {
  report: (startDate?: string, endDate?: string): Promise<ChargebackReport> => {
    let q = '/reports/chargebacks?'
    if (startDate) q += `start_date=${startDate}&`
    if (endDate) q += `end_date=${endDate}`
    return fetchAPI(q)
  },
}

// Orders invoice (blob download)
export const orderInvoice = async (orderId: number, orderNumber?: string): Promise<void> => {
  const res = await fetch(`${API_BASE}/orders/${orderId}/invoice.pdf`, { credentials: 'include' })
  if (!res.ok) throw new Error('Failed to generate invoice')
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `invoice_${orderNumber || orderId}.pdf`
  a.click()
  URL.revokeObjectURL(url)
}
