import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { BrandingProvider } from './BrandingContext'
import { LicenseProvider } from './LicenseContext'
import { I18nProvider } from './contexts/I18nContext'
import { OrgProvider } from './contexts/OrgContext'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 30, // 30 seconds
      refetchInterval: 1000 * 60, // 1 minute
    },
  },
})

// Register service worker for PWA install + push notifications
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <BrandingProvider><LicenseProvider><OrgProvider><I18nProvider><App /></I18nProvider></OrgProvider></LicenseProvider></BrandingProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
