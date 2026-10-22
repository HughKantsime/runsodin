import { fetchAPI } from './client'

export interface SquareMapping { product_id: number; location_id: string; variation_id: string; item_name: string; variation_name: string }
export interface SquareStatus { connected: boolean; revision: string | null; environment?: 'sandbox' | 'production'; merchant_id?: string; mappings: SquareMapping[] }
export interface SquareCatalog { revision: string; items: { variation_id: string; item_name: string; variation_name: string; sku: string | null }[]; cursor: string | null }
export interface SquareStock { revision: string; quantity: string | null; state: 'known' | 'unknown' | 'untracked'; retrieved_at: string; calculated_at: string | null }
const query = (revision: string, cursor?: string) => new URLSearchParams({ revision, ...(cursor ? { cursor } : {}) }).toString()
export const square = {
  status: () => fetchAPI<SquareStatus>('/products/square'),
  configure: (data: { access_token: string; environment: string; expected_revision: string | null }) => fetchAPI<SquareStatus>('/products/square', { method: 'PUT', body: JSON.stringify(data) }),
  disconnect: (revision: string) => fetchAPI<SquareStatus>(`/products/square?${query(revision)}`, { method: 'DELETE' }),
  locations: (revision: string) => fetchAPI<{ revision: string; locations: { id: string; name: string }[] }>(`/products/square/locations?${query(revision)}`),
  catalog: (revision: string, cursor?: string) => fetchAPI<SquareCatalog>(`/products/square/catalog?${query(revision, cursor)}`),
  map: (product: number, data: { revision: string; location_id: string; variation_id: string }) => fetchAPI<SquareStatus>(`/products/${product}/square`, { method: 'PUT', body: JSON.stringify(data) }),
  unmap: (product: number, revision: string) => fetchAPI<SquareStatus>(`/products/${product}/square?${query(revision)}`, { method: 'DELETE' }),
  stock: (product: number, revision: string) => fetchAPI<SquareStock>(`/products/${product}/square?${query(revision)}`),
}
