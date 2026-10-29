/** Display only: never use this value for dispatch or filename matching. */
export function displayName(row: { display_name?: unknown; item_name?: unknown; job_name?: unknown; print_name?: unknown; filename?: unknown } | null | undefined): string {
  for (const value of [row?.display_name, row?.item_name, row?.job_name, row?.print_name, row?.filename]) {
    if (typeof value === 'string' && value.trim() && !/odin-dispatch-[0-9a-f-]+/i.test(value)) return value.trim()
  }
  return 'Unnamed print'
}
