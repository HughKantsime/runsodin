import { expect, test, vi } from 'vitest'
const api = vi.hoisted(() => ({ fetch: vi.fn() }))
vi.mock('../../api/client', () => ({ fetchAPI: api.fetch }))
import { plugPowerOn, plugPowerOff, plugPowerToggle } from '../../api/printers'

test.each([plugPowerOn, plugPowerOff, plugPowerToggle])('manual client retains a nonce after a lost response', async command => {
 api.fetch.mockReset()
 api.fetch.mockRejectedValueOnce(new Error('Lost response'))
   .mockResolvedValue({accepted:true,power_state:true})
 await expect(command(599)).rejects.toThrow('Lost response')
 await command(599)
 const nonce = api.fetch.mock.calls[0][1].headers['X-Power-Request-Nonce']
 expect(nonce).toMatch(/^[0-9a-f]{32}$/)
 expect(api.fetch.mock.calls[1][1].headers['X-Power-Request-Nonce']).toBe(nonce)
 await command(599)
 expect(api.fetch.mock.calls[2][1].headers['X-Power-Request-Nonce']).not.toBe(nonce)
})

test('concurrent clicks share one request and one nonce', async () => {
 api.fetch.mockReset()
 let finish!: (value: unknown) => void
 api.fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
 const first = plugPowerToggle(600)
 const second = plugPowerToggle(600)
 expect(api.fetch).toHaveBeenCalledTimes(1)
 finish({accepted:true,power_state:false})
 expect(await first).toEqual(await second)
})
