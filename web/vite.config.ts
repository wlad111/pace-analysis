import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies the REST API to the FastAPI backend (karting.api.app).
// `make serve` accepts HOST/PORT, so the target has to be overridable too:
//   make serve PORT=9000
//   VITE_API_TARGET=http://127.0.0.1:9000 make web
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
})
