import { fetchAPI } from './client'

export interface SearchResult {
  id: number
  name: string
  type?: string
  qr_code?: string
  status?: string
}

export interface SearchResponse {
  orders: SearchResult[]
  products: SearchResult[]
  models: SearchResult[]
  jobs: SearchResult[]
  spools: SearchResult[]
  printers: SearchResult[]
}

export const search = {
  query: (q: string): Promise<SearchResponse> => fetchAPI('/search?q=' + encodeURIComponent(q)),
}
