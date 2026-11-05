/* Runs synchronously in <head>, before CSS/React. Keep in sync with themePreferences.ts. */
(() => {
  const read = key => { try { return localStorage.getItem(key) } catch { return null } }
  const stored = read('odin-theme')
  const mode = ['dark', 'light', 'high-contrast'].includes(stored)
    ? stored : (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
  const root = document.documentElement
  root.classList.toggle('light', mode === 'light')
  root.classList.toggle('high-contrast', mode === 'high-contrast')
  root.dataset.themeFamily = read('odin-theme-family') === 'foundry' && mode === 'dark' ? 'foundry' : 'classic'
})()
