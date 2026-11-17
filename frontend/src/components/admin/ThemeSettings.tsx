import ThemePicker from '../layout/ThemePicker'

export default function ThemeSettings() {
  return <section aria-labelledby="settings-theme" className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
    <h2 id="settings-theme" className="text-lg font-semibold mb-2">Theme</h2>
    <p className="text-sm text-[var(--brand-text-secondary)] mb-4">Theme family and density are saved on this device.</p>
    <div className="max-w-sm space-y-3"><ThemePicker /></div>
  </section>
}
