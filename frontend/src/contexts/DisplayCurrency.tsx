import { createContext, useContext, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { config } from '../api/system'
import { formatMoney } from '../utils/formatMoney'

export const DisplayCurrency = createContext<string | null>(null)

export function CurrencyProvider({ children }: { children: ReactNode }) {
  const query = useQuery({ queryKey: ['config'], queryFn: config.get, staleTime: 60000 })
  return <DisplayCurrency.Provider value={query.data?.currency ?? null}>{children}</DisplayCurrency.Provider>
}

export function useMoney() {
  const currency = useContext(DisplayCurrency)
  return (amount: unknown, unknown = 'Unknown', precision?: number) =>
    typeof amount === 'number' && Number.isFinite(amount) ? formatMoney(amount, currency, precision) : unknown
}
