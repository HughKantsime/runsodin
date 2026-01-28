/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Industrial/utilitarian palette for print farm vibes
        'farm': {
          50: '#f7f7f6',
          100: '#e5e4e1',
          200: '#cac8c3',
          300: '#a9a69e',
          400: '#8a8679',
          500: '#706c5f',
          600: '#58554a',
          700: '#47453d',
          800: '#3b3934',
          900: '#33312d',
          950: '#1a1917',
        },
        // Accent for active/printing states
        'print': {
          50: '#f0fdf4',
          100: '#dcfce7',
          200: '#bbf7d0',
          300: '#86efac',
          400: '#4ade80',
          500: '#22c55e',
          600: '#16a34a',
          700: '#15803d',
          800: '#166534',
          900: '#14532d',
        },
        // Status colors
        'status': {
          pending: '#fbbf24',    // amber
          scheduled: '#60a5fa',  // blue
          printing: '#34d399',   // emerald
          completed: '#a3a3a3', // neutral
          failed: '#f87171',     // red
        }
      },
      fontFamily: {
        'mono': ['JetBrains Mono', 'Fira Code', 'monospace'],
        'display': ['Space Grotesk', 'system-ui', 'sans-serif'],
        'body': ['Inter', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
