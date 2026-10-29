import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /v1 (REST and the live feeds) to the gateway, so the
// browser only ever talks to its own origin: no CORS_ORIGINS entry needed.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/v1': 'http://localhost:8080' },
  },
})
