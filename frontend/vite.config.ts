import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: process.env.CAD_API_BASE_URL || 'http://127.0.0.1:8000', changeOrigin: true, timeout: 0, proxyTimeout: 0 },
    },
  },
})
