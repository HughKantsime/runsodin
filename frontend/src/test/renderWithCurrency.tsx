import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { DisplayCurrency } from '../contexts/DisplayCurrency'

export function renderWithCurrency(ui: ReactNode) {
  return render(ui, { wrapper: ({ children }) => <DisplayCurrency.Provider value="EUR">{children}</DisplayCurrency.Provider> })
}
