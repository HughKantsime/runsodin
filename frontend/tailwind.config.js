/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Industrial/utilitarian palette - mapped to CSS variables for white-label branding
        // Fallback hex values ensure the app works without branding configured
        'farm': {
          50:  '#f7f7f6',
          100: 'var(--brand-text-primary, #e5e4e1)',
          200: '#cac8c3',
          300: '#a9a69e',
          400: 'var(--brand-sidebar-text, #8a8679)',
          500: 'var(--brand-text-muted, #706c5f)',
          600: 'var(--brand-text-muted, #58554a)',
          700: 'var(--brand-input-border, #47453d)',
          800: 'var(--brand-sidebar-active-bg, #3b3934)',
          900: 'var(--brand-card-bg, #33312d)',
          950: 'var(--brand-sidebar-bg, #1a1917)',
        },
        // Accent for active/printing states
        'print': {
          50:  '#f0fdf4',
          100: '#dcfce7',
          200: '#bbf7d0',
          300: '#86efac',
          400: 'var(--brand-accent, #4ade80)',
          500: 'var(--brand-primary, #22c55e)',
          600: 'var(--brand-primary, #16a34a)',
          700: '#15803d',
          800: '#166534',
          900: '#14532d',
        },
        // Status colors - never branded, always semantic
        'status': {
          pending:   '#fbbf24',  // amber
          scheduled: '#60a5fa',  // blue
          printing:  '#34d399',  // emerald
          completed: '#a3a3a3',  // neutral
          failed:    '#f87171',  // red
        }
      },
      fontFamily: {
        'mono':    ['var(--brand-font-mono)', 'ui-monospace', 'monospace'],
        'display': ['var(--brand-font-display)', 'system-ui', 'sans-serif'],
        'body':    ['var(--brand-font-body)', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
