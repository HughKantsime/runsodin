import tokens from '../design/tokens.json' with { type: 'json' };

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Farm colors use CSS variables — BrandingContext overrides these per-org
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
        // Print colors sourced from design/tokens.json
        'print': {
          50:  tokens.print['50'],
          100: tokens.print['100'],
          200: tokens.print['200'],
          300: tokens.print['300'],
          400: tokens.print['400'],
          500: 'var(--brand-primary, ' + tokens.print['500'] + ')',
          600: 'var(--brand-primary, ' + tokens.print['600'] + ')',
          700: tokens.print['700'],
          800: tokens.print['800'],
          900: tokens.print['900'],
        },
        // Status colors use CSS variables — BrandingContext overrides these per-org
        'status': {
          pending:   'var(--status-pending, ' + tokens.status.pending + ')',
          scheduled: 'var(--status-scheduled, ' + tokens.status.scheduled + ')',
          printing:  'var(--status-printing, ' + tokens.status.printing + ')',
          completed: 'var(--status-completed, ' + tokens.status.completed + ')',
          failed:    'var(--status-failed, ' + tokens.status.failed + ')',
        }
      },
      fontFamily: {
        'mono':    ['var(--brand-font-mono)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'display': ['var(--brand-font-display)', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        'body':    ['var(--brand-font-body)', '"IBM Plex Sans"', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        'DEFAULT': tokens.radius.default,
        'sm': tokens.radius.sm,
        'md': tokens.radius.md,
        'lg': tokens.radius.lg,
        'xl': tokens.radius.xl,
        '2xl': tokens.radius['2xl'],
      },
      fontSize: {
        'xs': [tokens.typography.fontSize.xs, { lineHeight: '1.5' }],
        'sm': [tokens.typography.fontSize.sm, { lineHeight: '1.5' }],
        'base': [tokens.typography.fontSize.base, { lineHeight: '1.6' }],
        'lg': [tokens.typography.fontSize.lg, { lineHeight: '1.5' }],
        'xl': [tokens.typography.fontSize.xl, { lineHeight: '1.3' }],
      },
    },
  },
  plugins: [],
}
