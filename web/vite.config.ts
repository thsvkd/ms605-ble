/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig, type ProxyOptions } from 'vite'

// The backend rejects a foreign Origin on writes and /ws (docs/GUI_API.md 4.3); a missing Origin passes.
const dropOrigin: ProxyOptions['configure'] = (proxy) => {
  proxy.on('proxyReq', (req) => req.removeHeader('origin'))
  proxy.on('proxyReqWs', (req) => req.removeHeader('origin'))
}

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../ms605/gui/static', emptyOutDir: true, assetsDir: 'assets' },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8605', changeOrigin: true, configure: dropOrigin },
      '/ws': { target: 'ws://127.0.0.1:8605', ws: true, changeOrigin: true, configure: dropOrigin },
    },
  },
  test: { environment: 'jsdom', setupFiles: ['src/test/setup.ts'] },
})
