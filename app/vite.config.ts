import { defineConfig } from 'vite'
import { fileURLToPath } from 'node:url'

// VITE_MOCK=1 → usa il mock SDK browser (src/dev-sdk-mock.ts) al posto dell'SDK reale,
// così l'app gira in un browser qualsiasi senza il simulatore desktop nativo.
const mock = process.env.VITE_MOCK
  ? { '@evenrealities/even_hub_sdk': fileURLToPath(new URL('./src/dev-sdk-mock.ts', import.meta.url)) }
  : {}

export default defineConfig({
  resolve: { alias: mock },
  server: { host: true },   // ascolta su 0.0.0.0: raggiungibile da VPN/LAN
})
