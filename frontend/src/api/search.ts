import { fetchAPI } from './client'

export interface SearchResult {
  type: string;
  id: number;
  title: string;
  subtitle: string | null;
  url: string;
  [key: string]: unknown;
}

export interface SearchResponse {
  results: SearchResult[];
  query: string;
  total: number;
}

export const search = {
  query: (q: string): Promise<SearchResponse> => fetchAPI('/search?q=' + encodeURIComponent(q)),
}
