/// <reference types="vitest/config" />
import path from 'node:path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { createDora } from './server/dora-server.js'

/**
 * In development, Dora's API runs inside Vite's own server against DORA_DIR (default ./playground),
 * so `npm run dev` is one process with hot reload.
 */
function doraApi(): Plugin {
  return {
    name: 'dora-api',
    configureServer(server) {
      if (process.env.VITEST) return
      const dora = createDora({ dir: path.resolve(process.env.DORA_DIR ?? 'playground') })
      server.middlewares.use((req, res, next) => { dora.handle(req, res, next) })
      server.httpServer?.on('close', () => dora.close())
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), doraApi()],
  server: { port: 5173, host: '127.0.0.1' },
  build: { chunkSizeWarningLimit: 2000 },
  test: { include: ['test/**/*.test.ts'] },
})
