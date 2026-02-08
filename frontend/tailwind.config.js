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
          50:  '#f4f6f9',
          100: '#e8ecf2',
          200: '#d4dae4',
          300: '#b8c2d0',
          400: '#8b95a8',
          500: '#4a5568',
          600: '#364155',
          700: '#252d3d',
          800: '#1a2030',
          900: '#0f1218',
          950: '#0a0c10',
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
