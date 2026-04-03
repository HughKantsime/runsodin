/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
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
        'print': {
          50:  '#FFF9EB',
          100: '#FFF0CC',
          200: '#FFDFA3',
          300: '#D4891F',
          400: '#C47A1A',
          500: 'var(--brand-primary, #C47A1A)',
          600: 'var(--brand-primary, #9A5E0D)',
          700: '#7A4B0A',
          800: '#5A3707',
          900: '#3A2404',
        },
        'status': {
          pending:   'var(--status-pending, #7A8396)',
          scheduled: 'var(--status-scheduled, #8B7BE8)',
          printing:  'var(--status-printing, #5B93E8)',
          completed: 'var(--status-completed, #3DAF5C)',
          failed:    'var(--status-failed, #D84848)',
        }
      },
      fontFamily: {
        'mono':    ['var(--brand-font-mono)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'display': ['var(--brand-font-display)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'body':    ['var(--brand-font-body)', '"IBM Plex Sans"', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        'DEFAULT': '6px',
        'sm': '4px',
        'md': '6px',
        'lg': '8px',
        'xl': '10px',
        '2xl': '12px',
      },
      fontSize: {
        'xs': ['11px', { lineHeight: '1.5' }],
        'sm': ['13px', { lineHeight: '1.5' }],
        'base': ['14px', { lineHeight: '1.6' }],
        'lg': ['16px', { lineHeight: '1.5' }],
        'xl': ['20px', { lineHeight: '1.3' }],
      },
    },
  },
  plugins: [],
}
