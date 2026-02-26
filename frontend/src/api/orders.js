import { fetchAPI } from './client.js'

const API_BASE = '/api'

// Products (v0.14.0)
export const products = {
  list: () => fetchAPI('/products'),
  get: (id) => fetchAPI(`/products/${id}`),
  create: (data) => fetchAPI('/products', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/products/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/products/${id}`, { method: 'DELETE' }),
  addComponent: (productId, data) => fetchAPI(`/products/${productId}/components`, { method: 'POST', body: JSON.stringify(data) }),
  removeComponent: (productId, componentId) => fetchAPI(`/products/${productId}/components/${componentId}`, { method: 'DELETE' }),
  addConsumable: (productId, data) => fetchAPI(`/products/${productId}/consumables`, { method: 'POST', body: JSON.stringify(data) }),
  removeConsumable: (productId, linkId) => fetchAPI(`/products/${productId}/consumables/${linkId}`, { method: 'DELETE' }),
}

// Orders (v0.14.0)
export const orders = {
  list: (status, platform) => {
    const params = new URLSearchParams()
    if (status) params.append('status_filter', status)
    if (platform) params.append('platform', platform)
    const query = params.toString()
    return fetchAPI('/orders' + (query ? '?' + query : ''))
  },
  get: (id) => fetchAPI(`/orders/${id}`),
  create: (data) => fetchAPI('/orders', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/orders/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/orders/${id}`, { method: 'DELETE' }),
  addItem: (orderId, data) => fetchAPI(`/orders/${orderId}/items`, { method: 'POST', body: JSON.stringify(data) }),
  updateItem: (orderId, itemId, data) => fetchAPI(`/orders/${orderId}/items/${itemId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  removeItem: (orderId, itemId) => fetchAPI(`/orders/${orderId}/items/${itemId}`, { method: 'DELETE' }),
  schedule: (id) => fetchAPI(`/orders/${id}/schedule`, { method: 'POST' }),
  ship: (id, data) => fetchAPI(`/orders/${id}/ship`, { method: 'PATCH', body: JSON.stringify(data) }),
}

// Pricing Config
export const pricingConfig = {
  get: () => fetchAPI('/pricing-config'),
  update: (data) => fetchAPI('/pricing-config', { method: 'PUT', body: JSON.stringify(data) }),
}

// Chargebacks
export const chargebacks = {
  report: (startDate, endDate) => {
    let q = '/reports/chargebacks?'
    if (startDate) q += `start_date=${startDate}&`
    if (endDate) q += `end_date=${endDate}`
    return fetchAPI(q)
  },
}

// Orders invoice (blob download)
export const orderInvoice = async (orderId, orderNumber) => {
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
