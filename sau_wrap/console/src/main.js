// SAU 控制台入口（设计文档 §6.1：Vue3 + hash 路由；只做 5409 API 的浏览器皮肤）
import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router.js'

createApp(App).use(router).mount('#app')
