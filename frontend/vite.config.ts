import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
export default defineConfig({ plugins:[react(),tailwindcss()], resolve:{alias:{'@':path.resolve(__dirname,'./src')}},build:{outDir:'../src/fincl/static',emptyOutDir:true,rollupOptions:{output:{manualChunks:{charts:['recharts'],react:['react','react-dom']}}}},test:{environment:'jsdom',globals:true,setupFiles:['./src/setupTests.ts'],css:false,testTimeout:15000,include:['src/**/*.test.{ts,tsx}'],exclude:['e2e/**','node_modules/**']}} as any)
