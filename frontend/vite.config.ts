import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
// `vite dev` only (scripts/dev.py): forward API calls to the reloading backend. The
// backend admits only its own Host and Origin, so the proxy presents both as its own.
const api = `http://127.0.0.1:${process.env.FINCL_API_PORT ?? '8765'}`
const proxied = { target:api, changeOrigin:true, headers:{origin:api} }
export default defineConfig({ plugins:[react(),tailwindcss()], resolve:{alias:{'@':path.resolve(__dirname,'./src')}},server:{proxy:{'/api':proxied,'/session':proxied,'/dev-login':proxied}},build:{outDir:'../src/fincl/static',emptyOutDir:true,rollupOptions:{output:{manualChunks:{charts:['recharts'],react:['react','react-dom']}}}},test:{environment:'jsdom',globals:true,setupFiles:['./src/setupTests.ts'],css:false,testTimeout:15000,include:['src/**/*.test.{ts,tsx}'],exclude:['e2e/**','node_modules/**']}} as any)
