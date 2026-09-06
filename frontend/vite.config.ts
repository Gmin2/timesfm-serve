import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'
import { weatherApiProxy } from './server/api-proxy.ts'
import { accountProxy } from './server/account-proxy.ts'
import { playgroundProxy } from './server/playground-proxy.ts'

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss(), ...(command === 'serve' ? [accountProxy(), playgroundProxy(), weatherApiProxy()] : [])],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: { host: '127.0.0.1', port: 5178, strictPort: true },
}))
