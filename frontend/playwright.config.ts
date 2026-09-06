import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e', fullyParallel: false,
  use: { baseURL: process.env.DASHBOARD_TEST_URL || 'http://127.0.0.1:5178',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {},
    viewport: { width: 1440, height: 1050 }, trace: 'retain-on-failure' },
})
