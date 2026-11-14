// Server times carry no zone and are UTC: every time a job row or its phone card shows must go
// through parseServerDate. Reading one with `new Date(...)` showed it hours off (found on the
// phone card's scheduled start, where the desktop row was right).
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from 'vitest'

test('scheduled, start and end times in the job row are never read as local time', () => {
  const source = readFileSync(resolve(__dirname, 'JobRow.tsx'), 'utf8')
  for (const field of ['scheduled_start', 'scheduled_end', 'actual_start', 'actual_end', 'created_at']) {
    expect(source).not.toMatch(new RegExp(`new Date\\(job\\.${field}\\b`))
  }
})
