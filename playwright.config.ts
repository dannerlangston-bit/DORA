import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: 'e2e',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: { baseURL: 'http://localhost:4411', viewport: { width: 1400, height: 860 } },
  webServer: { command: 'node e2e/serve.js', url: 'http://localhost:4411/api/state', reuseExistingServer: false },
})
