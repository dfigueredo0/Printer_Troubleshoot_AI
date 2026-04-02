import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
  test: {
    globals: false,
    environment: 'node',
    include: [
      'src/tests/**/*.test.ts',
      'src/tests/**/*.spec.ts',
    ],
    exclude: ['**/node_modules/**', '**/dist/**'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html', 'lcov'],
      reportsDirectory: '.claude-flow/coverage',
      thresholds: {
        lines:      80,
        functions:  80,
        branches:   75,
        statements: 80,
      },
      include: ['src/**/*.ts'],
      exclude: ['src/tests/**', 'src/**/*.d.ts'],
    },
    testTimeout:  10_000,
    hookTimeout:  15_000,
    reporters: process.env.CI ? ['verbose', 'json', 'junit'] : ['verbose'],
    outputFile: {
      json:  '.claude-flow/test-results/results.json',
      junit: '.claude-flow/test-results/junit.xml',
    },
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
      '@tests': resolve(__dirname, 'src/tests'),
    },
  },
});
