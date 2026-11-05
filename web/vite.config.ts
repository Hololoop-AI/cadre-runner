/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The Python fleet server (statusd.py) serves dist/ at /app/; the dev server
// proxies the live API so the app can be seen against real data.
const LIVE = 'http://127.0.0.1:8183';
const proxied = ['/api', '/fleet', '/session', '/tasks', '/surface-theme.css'];

export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: {
    port: 5183,
    strictPort: true,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: LIVE, changeOrigin: false }])),
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
