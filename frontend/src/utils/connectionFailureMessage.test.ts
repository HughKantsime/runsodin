import { describe, expect, it } from 'vitest'
import { CONNECTION_FAILURE_REASONS, connectionFailureHelp, connectionFailureMessage } from './shared'

const TYPES = ['bambu', 'moonraker', 'prusalink', 'octoprint', 'elegoo', 'ultimaker', 'anycubic_lan']
const SERVER = 'Server sentence.'

describe('connectionFailureMessage', () => {
  it('gives every known reason its own sentence and one thing to do, for every printer type', () => {
    for (const type of TYPES) {
      const known = CONNECTION_FAILURE_REASONS.filter(reason => reason !== 'unknown').map(reason => connectionFailureMessage(reason, type, SERVER))
      expect(new Set(known).size).toBe(known.length)
      for (const message of known) {
        expect(message).not.toContain(SERVER)
        expect(message.split('. ').length).toBeGreaterThanOrEqual(2) // what happened, then what to do
      }
    }
  })

  it('names the fix that fits the printer type when credentials are rejected', () => {
    expect(connectionFailureMessage('auth', 'bambu')).toMatch(/access code/)
    expect(connectionFailureMessage('auth', 'moonraker')).toMatch(/trusted_clients/)
    expect(connectionFailureMessage('auth', 'prusalink')).toMatch(/PrusaLink API key/)
    expect(connectionFailureMessage('auth', 'octoprint')).toMatch(/application key/)
    expect(connectionFailureMessage('unreachable', 'moonraker')).toMatch(/add the port/)
  })

  it.each(['unknown', undefined, null, 'something-new', { code: 'x' }])('keeps the server sentence and the checklist for %s', reason => {
    expect(connectionFailureMessage(reason, 'bambu', SERVER)).toBe(`${SERVER} ${connectionFailureHelp('bambu')}`)
    expect(connectionFailureMessage(reason, 'elegoo')).toBe(`The connection test failed and ODIN could not tell why. ${connectionFailureHelp('elegoo')}`)
  })
})
