import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'

export default [
  { ignores: ['dist/', 'node_modules/'] },
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: {
        ...globals.browser,
        ...globals.es2020,
      },
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...Object.fromEntries(
        Object.entries(js.configs.recommended.rules).map(([key, value]) => [
          key,
          value === 'error' || value === 2 ? 'warn' : value,
        ])
      ),
      ...Object.fromEntries(
        Object.entries(reactHooks.configs.recommended.rules).map(([key, value]) => [
          key,
          value === 'error' || value === 2 ? 'warn' : value,
        ])
      ),
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
    },
  },
]
