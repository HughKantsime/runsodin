import { fetchAPI } from './client.js'

export const search = {
  query: (q) => fetchAPI('/search?q=' + encodeURIComponent(q)),
}
