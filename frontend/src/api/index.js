import axios from 'axios'

const API_BASE = import.meta.env.VITE_API_URL || ''

const api = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
})

// ============== Printers ==============

export const printers = {
  list: (activeOnly = false) => 
    api.get('/api/printers', { params: { active_only: activeOnly } }).then(r => r.data),
  
  get: (id) => 
    api.get(`/api/printers/${id}`).then(r => r.data),
  
  create: (data) => 
    api.post('/api/printers', data).then(r => r.data),
  
  update: (id, data) => 
    api.patch(`/api/printers/${id}`, data).then(r => r.data),
  
  delete: (id) => 
    api.delete(`/api/printers/${id}`),
  
  updateSlot: (printerId, slotNumber, data) =>
    api.patch(`/api/printers/${printerId}/slots/${slotNumber}`, data).then(r => r.data),
}

// ============== Models ==============

export const models = {
  list: (category = null) => 
    api.get('/api/models', { params: { category } }).then(r => r.data),
  
  get: (id) => 
    api.get(`/api/models/${id}`).then(r => r.data),
  
  create: (data) => 
    api.post('/api/models', data).then(r => r.data),
  
  update: (id, data) => 
    api.patch(`/api/models/${id}`, data).then(r => r.data),
  
  delete: (id) => 
    api.delete(`/api/models/${id}`),
}

// ============== Jobs ==============

export const jobs = {
  list: (params = {}) => 
    api.get('/api/jobs', { params }).then(r => r.data),
  
  get: (id) => 
    api.get(`/api/jobs/${id}`).then(r => r.data),
  
  create: (data) => 
    api.post('/api/jobs', data).then(r => r.data),
  
  createBulk: (jobsData) =>
    api.post('/api/jobs/bulk', jobsData).then(r => r.data),
  
  update: (id, data) => 
    api.patch(`/api/jobs/${id}`, data).then(r => r.data),
  
  delete: (id) => 
    api.delete(`/api/jobs/${id}`),
  
  start: (id) =>
    api.post(`/api/jobs/${id}/start`).then(r => r.data),
  
  complete: (id) =>
    api.post(`/api/jobs/${id}/complete`).then(r => r.data),
  
  fail: (id, notes = null) =>
    api.post(`/api/jobs/${id}/fail`, null, { params: { notes } }).then(r => r.data),
  
  reset: (id) =>
    api.post(`/api/jobs/${id}/reset`).then(r => r.data),
}

// ============== Scheduler ==============

export const scheduler = {
  run: (config = null) =>
    api.post('/api/scheduler/run', config).then(r => r.data),
  
  getRuns: (limit = 30) =>
    api.get('/api/scheduler/runs', { params: { limit } }).then(r => r.data),
}

// ============== Timeline ==============

export const timeline = {
  get: (startDate = null, days = 7) =>
    api.get('/api/timeline', { params: { start_date: startDate, days } }).then(r => r.data),
}

// ============== Stats ==============

export const stats = {
  get: () =>
    api.get('/api/stats').then(r => r.data),
}

// ============== Health ==============

export const health = {
  check: () =>
    api.get('/health').then(r => r.data),
}

// ============== Spoolman ==============

export const spoolman = {
  sync: () =>
    api.post('/api/spoolman/sync').then(r => r.data),
  
  listSpools: () =>
    api.get('/api/spoolman/spools').then(r => r.data),
}

export default api
