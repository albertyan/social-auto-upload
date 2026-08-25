// hash 路由（设计文档 §6.1 定案：createWebHashHistory，静态托管无需服务端回退）
import { createRouter, createWebHashHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'status', component: () => import('./views/StatusView.vue') },
    { path: '/bind', name: 'bind', component: () => import('./views/BindView.vue') },
    { path: '/accounts', name: 'accounts', component: () => import('./views/AccountsView.vue') },
    { path: '/upgrade', name: 'upgrade', component: () => import('./views/UpgradeView.vue') },
    {
      path: '/unauthorized',
      name: 'unauthorized',
      component: () => import('./views/UnauthorizedView.vue'),
    },
  ],
})
