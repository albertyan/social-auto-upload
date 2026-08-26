// SAU 控制台 Vite 配置（设计文档 §6.6/§6.7/§6.8）
// - base './'：构建产物静态托管在 5409 的 /ui/ 子路径下（§6.4），资源用相对引用；
// - hash 路由（src/router.js 的 createWebHashHistory）：静态托管无需服务端路由回退；
// - dev 代理（§6.8）：vite dev（5173）将本地 API 转发到 127.0.0.1:5409；
//   开发模式无托盘票据链路，可设环境变量 SAU_DEV_TOKEN（local_token.bin 内容）
//   由代理注入 X-SAU-Local-Token 头——仅限开发机，生产链路一律票据换 Cookie。
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const API_PREFIXES = [
  '/status', '/config', '/bind', '/reload', '/nonce',
  '/machine-code', '/ui-ticket', '/browser',
  '/accounts', '/login', '/upgrade',
]

const devToken = process.env.SAU_DEV_TOKEN || ''

export default defineConfig({
  base: './',
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(API_PREFIXES.map((p) => [
      p,
      {
        target: 'http://127.0.0.1:5409',
        changeOrigin: true,
        configure: (proxy) => {
          if (devToken) {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('X-SAU-Local-Token', devToken)
            })
          }
        },
      },
    ])),
  },
})
