import js from '@eslint/js'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import tsParser from '@typescript-eslint/parser'
import prettier from 'eslint-config-prettier'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'

const TS_FILES = ['**/*.{ts,tsx}']

const scopeToTypeScript = (configs) => configs.map((config) => ({ ...config, files: TS_FILES }))

export default [
  {
    ignores: ['dist', 'coverage', 'node_modules', '*.config.js'],
  },
  {
    ...js.configs.recommended,
    files: TS_FILES,
  },
  ...scopeToTypeScript(tsPlugin.configs['flat/strict-type-checked']),
  ...scopeToTypeScript(tsPlugin.configs['flat/stylistic-type-checked']),
  {
    files: TS_FILES,
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    ...react.configs.flat.recommended,
    files: TS_FILES,
    settings: { react: { version: 'detect' } },
  },
  {
    ...react.configs.flat['jsx-runtime'],
    files: TS_FILES,
  },
  {
    files: TS_FILES,
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
      'react/prop-types': 'off',
      // cmdk styles its input wrapper through this custom attribute.
      'react/no-unknown-property': ['error', { ignore: ['cmdk-input-wrapper'] }],
      '@typescript-eslint/consistent-type-imports': 'error',
      eqeqeq: ['error', 'always'],
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
  prettier,
]
