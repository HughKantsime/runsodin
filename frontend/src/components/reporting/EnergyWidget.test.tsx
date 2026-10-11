import { StrictMode } from 'react'
import { beforeEach, expect, test, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import EnergyWidget from './EnergyWidget'
import { getEnergyRate, setEnergyRate } from '../../api'
vi.mock('../../api', () => ({ getEnergyRate: vi.fn(), setEnergyRate: vi.fn() }))
vi.mock('recharts', () => ({
  ResponsiveContainer: ({children}: any) => <div>{children}</div>,
  BarChart: ({children}: any) => <div>{children}</div>,
  Bar: () => null, XAxis: () => null, YAxis: () => null, CartesianGrid: () => null, Tooltip: () => null,
}))
const jobs = [{energy_kwh: 2, completed_at:'2026-10-11T12:00:00Z'}]
beforeEach(() => {
  vi.mocked(getEnergyRate).mockReset().mockResolvedValue({energy_cost_per_kwh:0.12})
  vi.mocked(setEnergyRate).mockReset().mockResolvedValue({energy_cost_per_kwh:0})
})
test('stored zero renders zero cost and remains zero in editor', async () => {
  vi.mocked(getEnergyRate).mockResolvedValue({energy_cost_per_kwh:0})
  render(<EnergyWidget jobs={jobs}/>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0\/kWh/}))
  expect(screen.getByText('$0.00')).toBeInTheDocument()
  expect(screen.getByRole('spinbutton')).toHaveValue(0)
})
test('load failure shows unavailable cost and allows retry', async () => {
  vi.mocked(getEnergyRate).mockRejectedValueOnce(new Error('Synthetic unavailable'))
  render(<EnergyWidget jobs={jobs}/>)
  expect(await screen.findByText('Energy rate unavailable')).toBeInTheDocument()
  expect(screen.queryByText('$0.24')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Retry'}))
  expect(await screen.findByRole('button',{name:/\$0.12\/kWh/})).toBeInTheDocument()
})
test('save failure keeps editor and saved rate with visible feedback', async () => {
  vi.mocked(setEnergyRate).mockRejectedValue(new Error('Synthetic save failed'))
  render(<EnergyWidget jobs={jobs}/>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0.12\/kWh/}))
  fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'0.30'}})
  fireEvent.click(screen.getByRole('button',{name:'Save'}))
  expect(await screen.findByText('Could not save energy rate')).toBeInTheDocument()
  expect(screen.getByRole('spinbutton')).toBeInTheDocument()
  expect(screen.getByText('$0.24')).toBeInTheDocument()
  expect(setEnergyRate).toHaveBeenCalledWith(0.3)
})

test.each([NaN, Infinity, -1, '0', null])('invalid API rate %s does not become a cost', async value => {
  vi.mocked(getEnergyRate).mockResolvedValue({energy_cost_per_kwh:value} as any)
  render(<EnergyWidget jobs={jobs}/>)
  expect(await screen.findByText('Energy rate unavailable')).toBeInTheDocument()
  expect(screen.queryByText('$0.24')).not.toBeInTheDocument()
})
test('saving zero uses the confirmed response', async () => {
  render(<EnergyWidget jobs={jobs}/>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0.12\/kWh/}))
  fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'0'}})
  fireEvent.click(screen.getByRole('button',{name:'Save'}))
  expect(await screen.findByRole('button',{name:/\$0\/kWh/})).toBeInTheDocument()
  expect(setEnergyRate).toHaveBeenCalledWith(0)
  expect(screen.getByText('$0.00')).toBeInTheDocument()
})
test('empty editor does not submit a guessed zero', async () => {
  render(<EnergyWidget jobs={jobs}/>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0.12\/kWh/}))
  fireEvent.change(screen.getByRole('spinbutton'),{target:{value:''}})
  fireEvent.click(screen.getByRole('button',{name:'Save'}))
  expect(await screen.findByText('Enter a finite nonnegative energy rate')).toBeInTheDocument()
  expect(setEnergyRate).not.toHaveBeenCalled()
})
test('invalid save response leaves confirmed cost and editor intact', async () => {
  vi.mocked(setEnergyRate).mockResolvedValue({energy_cost_per_kwh:Infinity})
  render(<EnergyWidget jobs={jobs}/>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0.12\/kWh/}))
  fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'0.3'}})
  fireEvent.click(screen.getByRole('button',{name:'Save'}))
  expect(await screen.findByText('Could not save energy rate')).toBeInTheDocument()
  expect(screen.getByText('$0.24')).toBeInTheDocument()
})

test('obsolete load cannot overwrite a confirmed save', async () => {
  let completeOld!: (value: any) => void
  vi.mocked(getEnergyRate).mockImplementationOnce(() => new Promise(resolve => {completeOld=resolve}))
  render(<StrictMode><EnergyWidget jobs={jobs}/></StrictMode>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0.12\/kWh/}))
  fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'0'}})
  fireEvent.click(screen.getByRole('button',{name:'Save'}))
  expect(await screen.findByRole('button',{name:/\$0\/kWh/})).toBeInTheDocument()
  await act(async () => completeOld({energy_cost_per_kwh:9}))
  expect(screen.getByRole('button',{name:/\$0\/kWh/})).toBeInTheDocument()
  expect(screen.getByText('$0.00')).toBeInTheDocument()
})
test('pending save disables duplicate submission and cancel', async () => {
  let completeSave!: (value: any) => void
  vi.mocked(setEnergyRate).mockImplementation(() => new Promise(resolve => {completeSave=resolve}))
  render(<EnergyWidget jobs={jobs}/>)
  fireEvent.click(await screen.findByRole('button',{name:/\$0.12\/kWh/}))
  fireEvent.click(screen.getByRole('button',{name:'Save'}))
  const pending=screen.getByRole('button',{name:'Saving...'})
  expect(pending).toBeDisabled()
  expect(screen.getByRole('button',{name:'Cancel'})).toBeDisabled()
  expect(screen.getByRole('spinbutton')).toBeDisabled()
  fireEvent.click(pending)
  expect(setEnergyRate).toHaveBeenCalledTimes(1)
  await act(async () => completeSave({energy_cost_per_kwh:0.12}))
  expect(screen.getByRole('button',{name:/\$0.12\/kWh/})).toBeInTheDocument()
})
