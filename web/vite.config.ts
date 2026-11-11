import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const mock = mode === 'mock' || env.VITE_MOCK === '1'
  if (command === 'build' && mock) {
    throw new Error('构建产物不得包含模拟数据：请去掉 VITE_MOCK 或 --mode mock 后再构建')
  }
  return {
    plugins: [vue()],
    define: { __MOCK__: JSON.stringify(mock) },
    server: {
      proxy: { '/api': { target: env.CONSOLE_API_TARGET || 'http://127.0.0.1:8000' } },
    },
    build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 1600 },
    test: { environment: 'jsdom', include: ['tests/**/*.test.ts'], setupFiles: ['tests/setup.ts'] },
  }
})
