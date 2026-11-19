export function fjordSettingsTab(page, name) {
  return page.locator('main [data-scrolltabs]').getByRole('button', { name, exact: true })
}
