/* Runs synchronously in <head>, before CSS/React. Keep in sync with themePreferences.ts. */
(() => {
  const read = key => { try { return localStorage.getItem(key) } catch { return null } }
  const stored = read('odin-theme')
  const mode = ['dark', 'light', 'high-contrast'].includes(stored)
    ? stored : (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
  const root = document.documentElement
  root.classList.toggle('light', mode === 'light')
  root.classList.toggle('high-contrast', mode === 'high-contrast')
  const family = read('odin-theme-family')
  root.dataset.themeFamily = mode !== 'high-contrast' && (family === 'fjord' || (family === 'foundry' && mode === 'dark')) ? family : 'classic'
  if (root.dataset.themeFamily === 'fjord') root.dataset.fjordDensity = read('odin-foundry-density') === 'compact' ? 'compact' : 'comfortable'
  else delete root.dataset.fjordDensity
})()
