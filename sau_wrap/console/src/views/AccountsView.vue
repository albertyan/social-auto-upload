<!-- 账号页：/status 中 accounts 快照展示（本步范围边界：登录扫码会话链路 §6.5
     不在本步实施——登录按钮置灰提示「登录功能建设中」，/login/* 保持 501 占位） -->
<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { apiGet, UnauthorizedError } from '../api.js'

const accounts = ref([])
const err = ref('')
let timer = null

async function refresh() {
  try {
    const s = await apiGet('/status')
    accounts.value = s.accounts || []
    err.value = ''
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="card">
    <h2>平台账号（快照，来自 /status）</h2>
    <p v-if="err" class="msg err">{{ err }}</p>
    <table v-if="accounts.length">
      <tr>
        <th>平台</th>
        <th>账号</th>
        <th>状态</th>
        <th>操作</th>
      </tr>
      <tr v-for="acc in accounts" :key="acc.platform_key + ':' + acc.account_name">
        <td>{{ acc.platform_key }}</td>
        <td>{{ acc.account_name }}</td>
        <td>
          <span class="badge" :class="acc.is_valid ? 'ok' : 'bad'">
            {{ acc.is_valid ? '有效' : '无效/过期' }}
          </span>
        </td>
        <td>
          <button disabled title="登录功能建设中">登录</button>
        </td>
      </tr>
    </table>
    <p v-else style="color:#7f8c9b">
      暂无账号。登录功能建设中（扫码登录会话链路将在后续步骤上线）。
    </p>
  </div>
</template>
