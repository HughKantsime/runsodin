import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import AmsEnvironmentChart from './AmsEnvironmentChart'
import { getAmsEnvironment } from '../../api'
vi.mock('../../api', () => ({ getAmsEnvironment: vi.fn() }))
vi.mock('recharts', () => ({
 ResponsiveContainer: ({children}: any) => <div>{children}</div>,
 LineChart: ({data, children}: any) => <div><div data-testid="chart-values">{JSON.stringify(data)}</div>{children}</div>,
 Line: ({name}: any) => <span>{name}</span>, XAxis: () => null, YAxis: () => null, CartesianGrid: () => null, Tooltip: () => null, Legend: () => null,
}))
beforeEach(() => vi.mocked(getAmsEnvironment).mockResolvedValue({printer_id:1,hours:24,units:{}}))
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('does not claim empty data after a failed environment request', async () => {
 vi.mocked(getAmsEnvironment).mockRejectedValue(new Error('Synthetic failure'))
 render(<AmsEnvironmentChart printerId={1} />)
 expect(await screen.findByRole('alert')).toBeTruthy()
 expect(screen.queryByText('No AMS environment data available')).toBeNull()
})
it('keeps AMS unit identity available instead of flattening incompatible readings', async () => {
 vi.mocked(getAmsEnvironment).mockResolvedValue({printer_id:1,hours:24,units:{'0':[{time:'2026-10-11T12:00:00Z',temperature:20,humidity:2}],'1':[{time:'2026-10-11T12:00:00Z',temperature:30,humidity:4}]}})
 render(<AmsEnvironmentChart printerId={1} />)
 expect(await screen.findByRole('combobox', {name:'AMS unit'})).toBeTruthy()
})

it('plots only the selected unit while preserving zero readings', async () => {
 vi.mocked(getAmsEnvironment).mockResolvedValue({printer_id:1,hours:24,units:{'0':[{time:'2026-10-11T12:00:00Z',temperature:0,humidity:0}],'1':[{time:'2026-10-11T12:00:00Z',temperature:30,humidity:4}]}})
 render(<AmsEnvironmentChart printerId={1} />)
 const select = await screen.findByRole('combobox', {name:'AMS unit'})
 expect(JSON.parse(screen.getByTestId('chart-values').textContent!)[0].temp).toBe(0)
 fireEvent.change(select, {target:{value:'1'}})
 const values = JSON.parse(screen.getByTestId('chart-values').textContent!)
 expect(values.length).toBe(1)
 expect(values[0].temp).toBe(30)
})
it('labels explicitly reported humidity levels without inventing a percentage', async () => {
 vi.mocked(getAmsEnvironment).mockResolvedValue({printer_id:1,hours:24,humidity_unit:'level',units:{'0':[{time:'2026-10-11T12:00:00Z',temperature:20,humidity:3}]}})
 render(<AmsEnvironmentChart printerId={1} />)
 expect(await screen.findByText('Humidity level (1–5)')).toBeTruthy()
 expect(screen.queryByText('Humidity (%)')).toBeNull()
})
it('does not infer humidity units from low numeric values in older responses', async () => {
 vi.mocked(getAmsEnvironment).mockResolvedValue({printer_id:1,hours:24,units:{'0':[{time:'2026-10-11T12:00:00Z',temperature:20,humidity:3}]}})
 render(<AmsEnvironmentChart printerId={1} />)
 expect(await screen.findByText('Humidity (reported value)')).toBeTruthy()
})
it('can retry an unsuccessful request', async () => {
 vi.mocked(getAmsEnvironment).mockRejectedValueOnce(new Error('Synthetic failure')).mockResolvedValueOnce({printer_id:1,hours:24,units:{}})
 render(<AmsEnvironmentChart printerId={1} />)
 await screen.findByRole('alert')
 fireEvent.click(screen.getByRole('button', {name:'Retry'}))
 await screen.findByText('No AMS environment data available')
 expect(screen.queryByRole('alert')).toBeNull()
})
it('ignores the previous printer response after a switch', async () => {
 let finish: (value:any)=>void = () => {}
 vi.mocked(getAmsEnvironment).mockImplementation(id => id===1 ? new Promise(resolve=>{finish=resolve}) : Promise.resolve({printer_id:2,hours:24,units:{}}))
 const view=render(<AmsEnvironmentChart printerId={1} />)
 view.rerender(<AmsEnvironmentChart printerId={2} />)
 await screen.findByText('No AMS environment data available')
 await act(async()=>finish({printer_id:1,hours:24,units:{'0':[{time:'2026-10-11T12:00:00Z',temperature:20,humidity:3}]}}))
 expect(screen.queryByTestId('chart-values')).toBeNull()
})

it('orders readings by UTC instant and excludes invalid timestamps', async () => {
 vi.mocked(getAmsEnvironment).mockResolvedValue({printer_id:1,hours:24,units:{'0':[
  {time:'2026-10-11T13:00:00',temperature:22,humidity:2},
  {time:'2026-10-11T08:00:00-04:00',temperature:20,humidity:2},
  {time:'invalid',temperature:99,humidity:2},
 ]}})
 render(<AmsEnvironmentChart printerId={1} />)
 const chart=await screen.findByTestId('chart-values')
 const values=JSON.parse(chart.textContent!)
 expect(values.map((v:any)=>v.temp)).toEqual([20,22])
 expect(values.map((v:any)=>v.timestamp)).toEqual([Date.parse('2026-10-11T12:00:00Z'),Date.parse('2026-10-11T13:00:00Z')])
 expect(chart.textContent).not.toContain('Invalid Date')
})
it('ignores a late response from the previous time window', async () => {
 let finish: (value:any)=>void = () => {}
 vi.mocked(getAmsEnvironment).mockImplementation((_id,hours)=>hours===24 ? new Promise(resolve=>{finish=resolve}) : Promise.resolve({printer_id:1,hours:6,units:{}}))
 render(<AmsEnvironmentChart printerId={1} />)
 fireEvent.click(screen.getByRole('button', {name:'6h'}))
 await screen.findByText('No AMS environment data available')
 await act(async()=>finish({printer_id:1,hours:24,units:{'0':[{time:'2026-10-11T12:00:00Z',temperature:20,humidity:3}]}}))
 expect(screen.queryByTestId('chart-values')).toBeNull()
})
