import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { visualizer } from 'rollup-plugin-visualizer'
import fs from 'fs'
import path from 'path'

const version = fs.readFileSync(path.resolve(__dirname, '../VERSION'), 'utf-8').trim()

export function buildDependencyExclusion() {
  return {
    name: 'exclude-build-only-dependencies',
    generateBundle() {
      const modules = [...this.getModuleIds()]
      if (modules.some(id => /node_modules\/(braces|chokidar|fast-glob|micromatch|tailwindcss)\//.test(id.replaceAll('\\', '/')))) {
        this.error('Build-only dependency entered the browser bundle')
      }
      this.emitFile({ type: 'asset', fileName: 'build-dependency-exclusion.json',
        source: JSON.stringify({ status: 'pass', checked_modules: modules.length, forbidden_modules: 0 }) })
    },
  }
}

export default defineConfig({
  plugins: [
    react(),
    buildDependencyExclusion(),
    process.env.ANALYZE &&
      visualizer({
        filename: 'dist/stats.html',
        open: false,
        gzipSize: true,
        brotliSize: true,
      }),
  ].filter(Boolean),
  define: {
    __APP_VERSION__: JSON.stringify(version),
  },
  server: {
    port: 3000,
    allowedHosts: ['odin.subsystem.app'],
    proxy: {
      '/static': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
