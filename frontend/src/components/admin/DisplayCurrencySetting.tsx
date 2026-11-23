import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchAPI } from '../../api/client'
import { Button, Card } from '../ui'

export default function DisplayCurrencySetting({ saved, global }: { saved: string | null; global: boolean }) {
  const [chosen, setChosen] = useState(saved ?? '')
  const cache = useQueryClient()
  const save = useMutation({
    mutationFn: () => fetchAPI('/orders/finance-settings' + (global ? '?global_scope=true' : ''), {
      method: 'PUT', body: JSON.stringify({ currency: chosen || null }),
    }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ['config'] }),
  })
  return <Card padding="lg">
    <label className="block font-semibold">Display currency
      <select className="ml-3" value={chosen} onChange={e => setChosen(e.target.value)}>
        <option value="">Not set</option>
        {['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'NZD'].map(code => <option key={code}>{code}</option>)}
      </select>
    </label>
    <p className="my-2 text-sm">This currency labels amounts without converting them. Earlier estimates used the price settings in effect when they were saved.</p>
    <Button onClick={() => save.mutate()} disabled={save.isPending || chosen === (saved ?? '')}>Save currency</Button>
    {save.isError && <p role="alert">{save.error.message}</p>}
  </Card>
}
