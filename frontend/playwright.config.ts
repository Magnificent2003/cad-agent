import { defineConfig } from '@playwright/test'

// Local test traffic must bypass workstation HTTP proxies.
const bypass = [process.env.NO_PROXY, process.env.no_proxy, '127.0.0.1,localhost,::1']
  .filter(Boolean)
  .join(',')
process.env.NO_PROXY = bypass
process.env.no_proxy = bypass

const port = Number(process.env.PLAYWRIGHT_PORT || 15173)

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  use: { baseURL: `http://127.0.0.1:${port}`, viewport: { width: 1440, height: 1000 } },
  webServer: {
    command: `npm run dev -- --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
  },
})
