/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import { Agent } from 'node:https'
import { homedir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, type ProxyOptions } from 'vite'

// The backend rejects a foreign Origin on writes and /ws (docs/GUI_API.md 4.3); a missing Origin passes.
const dropOrigin: ProxyOptions['configure'] = (proxy) => {
  proxy.on('proxyReq', (req) => req.removeHeader('origin'))
  proxy.on('proxyReqWs', (req) => req.removeHeader('origin'))
}

export default defineConfig(({ command, mode }) => {
  let https: { cert: Buffer; key: Buffer } | undefined
  let agent: Agent | undefined
  if (command === 'serve' && mode !== 'test') {
    const projectRoot = fileURLToPath(new URL('..', import.meta.url))
    const dataRoot = process.env.MS605_DATA_DIR?.replace(/^~(?=\/|$)/, homedir())
      ?? projectRoot
    const tlsRoot = resolve(projectRoot, dataRoot, 'cal_results/gui_tls')
    try {
      https = { cert: readFileSync(resolve(tlsRoot, 'cert.pem')), key: readFileSync(resolve(tlsRoot, 'key.pem')) }
      agent = new Agent({ ca: readFileSync(resolve(tlsRoot, 'ca.pem')) })
    } catch {
      throw new Error(`Run make gui first to prepare HTTPS certificates in ${tlsRoot}. Use the same MS605_DATA_DIR.`)
    }
  }
  return {
    plugins: [react()],
    build: { outDir: '../ms605/gui/static', emptyOutDir: true, assetsDir: 'assets' },
    server: {
      port: 5173,
      https,
      proxy: {
        '/api': { target: 'https://127.0.0.1:8605', agent, changeOrigin: true, configure: dropOrigin },
        '/ws': { target: 'wss://127.0.0.1:8605', agent, ws: true, changeOrigin: true, configure: dropOrigin },
      },
    },
    test: { environment: 'jsdom', setupFiles: ['src/test/setup.ts'] },
  }
})
