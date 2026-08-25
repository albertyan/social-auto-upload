<!-- 控制台框架：顶部导航 + 路由视图（设计文档 §6.1：原生样式，不引组件库） -->
<script setup>
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <header class="topbar">
    <span class="brand">SAU Agent 控制台</span>
    <nav>
      <RouterLink to="/">状态总览</RouterLink>
      <RouterLink to="/bind">绑定</RouterLink>
      <RouterLink to="/accounts">账号</RouterLink>
      <RouterLink to="/upgrade">升级</RouterLink>
    </nav>
  </header>
  <main class="content">
    <RouterView />
  </main>
</template>

<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: #f5f6f7;
  color: #2c3e50;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 24px;
  background: #1f2d3d;
  color: #fff;
  padding: 10px 24px;
}
.brand { font-weight: 600; font-size: 16px; }
.topbar nav { display: flex; gap: 16px; }
.topbar a {
  color: #cfd8e3;
  text-decoration: none;
  padding: 4px 10px;
  border-radius: 4px;
}
.topbar a.router-link-active { background: #3b82f6; color: #fff; }
.content { max-width: 900px; margin: 24px auto; padding: 0 16px; }
.card {
  background: #fff;
  border-radius: 8px;
  padding: 20px 24px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
  margin-bottom: 16px;
}
h2 { font-size: 16px; margin: 0 0 12px; }
table { border-collapse: collapse; width: 100%; }
td, th {
  border-bottom: 1px solid #eef0f2;
  padding: 8px 10px;
  text-align: left;
  font-size: 14px;
}
th { color: #7f8c9b; font-weight: 500; width: 180px; }
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
}
.badge.ok { background: #e6f7ee; color: #16a34a; }
.badge.bad { background: #fdecec; color: #dc2626; }
.badge.warn { background: #fff7e0; color: #b45309; }
button {
  background: #3b82f6;
  border: none;
  color: #fff;
  padding: 8px 18px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}
button:disabled { background: #c3cdd9; cursor: not-allowed; }
button.secondary { background: #64748b; }
input {
  padding: 8px 10px;
  border: 1px solid #d5dbe3;
  border-radius: 6px;
  width: 100%;
  font-size: 14px;
}
.field { margin-bottom: 12px; }
.field label { display: block; font-size: 13px; color: #556; margin-bottom: 4px; }
.msg { font-size: 13px; margin-top: 8px; }
.msg.ok { color: #16a34a; }
.msg.err { color: #dc2626; }
</style>
