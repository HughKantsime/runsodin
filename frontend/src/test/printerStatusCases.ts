// These cases use the component tests' fixed clock (2026-10-11T12:00:00Z).
const expired = { last_seen: '2026-10-11T11:58:30Z' }
const hmsFault = { hms_errors: [{ severity: 'error' }] }

export const printerStatusCases = [
  { name: 'error', report: { gcode_state: 'ERROR' }, dashboard: 'Error', footer: 'Error', foundry: 'Needs you' },
  { name: 'runout', report: { gcode_state: 'RUNOUT' }, dashboard: 'Error', footer: 'Error', foundry: 'RUNOUT' },
  { name: 'active failure', report: { gcode_state: 'FAILED', state_needs_attention: true }, dashboard: 'Error', footer: 'Error', foundry: 'Needs you' },
  { name: 'cleared failure', report: { gcode_state: 'FAILED', state_needs_attention: false }, dashboard: 'Idle', footer: 'Online', foundry: 'Ready' },
  { name: 'failure without attention field', report: { gcode_state: 'FAILED' }, dashboard: 'Error', footer: 'Error', foundry: 'Needs you' },
  { name: 'offline error', report: { gcode_state: 'ERROR', ...expired }, dashboard: 'Offline', footer: 'Offline', foundry: 'Offline' },
  { name: 'paused', report: { gcode_state: 'PAUSE' }, dashboard: 'Printing', footer: 'Online', foundry: 'Needs you' },
  { name: 'running', report: { gcode_state: 'RUNNING' }, dashboard: 'Printing', footer: 'Online', foundry: 'Printing' },
  { name: 'idle', report: { gcode_state: 'IDLE' }, dashboard: 'Idle', footer: 'Online', foundry: 'Ready' },
  { name: 'HMS fault during print', report: { gcode_state: 'RUNNING', ...hmsFault }, dashboard: 'Error', footer: 'Online', foundry: 'Needs you' },
  { name: 'offline HMS fault', report: { gcode_state: 'IDLE', ...expired, ...hmsFault }, dashboard: 'Error', footer: 'Offline', foundry: 'Offline' },
]
