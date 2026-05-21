/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './web/src/**/*.html',
    './web/src/**/*.js',
    './web/src/themes/**/*.html',
    './web/src/themes/**/*.css',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['IBM Plex Sans', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['IBM Plex Mono', 'ui-monospace', 'monospace'],
      },
      colors: {
        brand: {
          50:  '#fff4ed',
          100: '#ffe6d4',
          500: '#ff5722',
          600: '#f7440f',
          700: '#cc3508',
        },
        ink: {
          50:  '#f7f8fa',
          100: '#eef0f4',
          200: '#dde1ea',
          300: '#b8c0cc',
          500: '#5b6677',
          700: '#2a3340',
          900: '#0e131b',
        },
      },
      boxShadow: {
        card: '0 1px 2px rgba(15,23,42,.04), 0 8px 24px -8px rgba(15,23,42,.08)',
      },
    },
  },
  // Classes assembled dynamically at runtime (string concatenation) — Tailwind
  // cannot detect these via static scan, so we safelist them explicitly.
  safelist: [
    // grid-cols used in cardGrid() and the quarters table (4 quarters + overtime)
    { pattern: /^grid-cols-(1|2|3|4|5|6|7|8)$/ },
    // gap used dynamically via gap-${gap}
    { pattern: /^gap-(2|3|4|5|6)$/ },
    // responsive table columns: hide on mobile, restore as table-cell on sm+
    'sm:table-cell',
    'sm:hidden',
  ],
  plugins: [],
};
