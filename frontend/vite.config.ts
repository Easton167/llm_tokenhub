import type { IncomingMessage } from 'node:http'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = 'http://localhost:8000'

/**
 * 开发代理：浏览器刷新 SPA 路由（如 /keys、/usage）会带 Accept: text/html，
 * 若直接转发到后端 REST 会返回 401 JSON 而非 index.html。
 */
function devApiProxy() {
  return {
    target: BACKEND,
    bypass(req: IncomingMessage) {
      const accept = req.headers.accept ?? ''
      if (accept.includes('text/html')) {
        return '/index.html'
      }
    },
  }
}

const apiProxy = {
  '/auth': devApiProxy(),
  '/keys': devApiProxy(),
  '/kb': devApiProxy(),
  '/wallets': devApiProxy(),
  '/usage': devApiProxy(),
  '/orgs': devApiProxy(),
  '/payments': devApiProxy(),
  '/chat': devApiProxy(),
  '/v1': devApiProxy(),
  '/health': devApiProxy(),
  '/admin': devApiProxy(),
}

export default defineConfig({
  plugins: [react()],
  server: {
    // 监听所有网卡，局域网可通过本机 IP 访问（如 http://172.16.20.131:8088）
    host: '0.0.0.0',
    port: 8088,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 8088,
    strictPort: true,
    proxy: apiProxy,
  },
})
