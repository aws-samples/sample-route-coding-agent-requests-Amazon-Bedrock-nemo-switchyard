import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      'node_modules/',
      'cdk.out/',
      'coverage/',
      '**/*.js',
      '**/*.d.ts',
      'bench/',
      'docker/',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/explicit-module-boundary-types': 'error',
      'no-console': 'off',
    },
  },
  {
    files: ['eslint.config.mjs'],
    extends: [tseslint.configs.disableTypeChecked],
  },
  prettier,
);
