/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // O.D.I.N. "Forge" palette — industrial dark with amber accents
        'farm': {
          50:  'rgb(var(--farm-50) / <alpha-value>)',
          100: 'rgb(var(--farm-100) / <alpha-value>)',
          200: 'rgb(var(--farm-200) / <alpha-value>)',
          300: 'rgb(var(--farm-300) / <alpha-value>)',
          400: 'rgb(var(--farm-400) / <alpha-value>)',
          500: 'rgb(var(--farm-500) / <alpha-value>)',
          600: 'rgb(var(--farm-600) / <alpha-value>)',
          700: 'rgb(var(--farm-700) / <alpha-value>)',
          800: 'rgb(var(--farm-800) / <alpha-value>)',
          900: 'rgb(var(--farm-900) / <alpha-value>)',
          950: 'rgb(var(--farm-950) / <alpha-value>)',
        },
        // Accent — amber/bronze (Odin's forge)
        'print': {
          50:  '#fffbeb',
          100: '#fef3c7',
          200: '#fde68a',
          300: '#fbbf24',
          400: '#f59e0b',
          500: 'var(--brand-primary, #d97706)',
          600: 'var(--brand-primary, #b45309)',
          700: '#92400e',
          800: '#78350f',
          900: '#451a03',
        },
        // Status colors — semantic, never branded
        'status': {
          pending:   '#6b7280',
          scheduled: '#8b5cf6',
          printing:  '#3b82f6',
          completed: '#22c55e',
          failed:    '#ef4444',
        }
      },
      fontFamily: {
        'mono':    ['var(--brand-font-mono)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'display': ['var(--brand-font-display)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'body':    ['var(--brand-font-body)', '"IBM Plex Sans"', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        'DEFAULT': '4px',
        'sm': '2px',
        'md': '4px',
        'lg': '6px',
        'xl': '8px',
        '2xl': '10px',
      },
    },
  },
  plugins: [],
}
