<!-- 状态总览（首页）：GET /status 全字段展示，5s 自动刷新（设计文档 §6.2） -->
<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { apiGet, UnauthorizedError } from '../api.js'

const status = ref(null)
const err = ref('')
const updatedAt = ref('')
let timer = null

const connBadge = computed(() => {
  const s = status.value
  if (!s) return { cls: 'warn', text: '加载中' }
  if (s.suspended) return { cls: 'warn', text: '已挂起（4401 待重绑）' }
  if (s.ws_connected) return { cls: 'ok', text: '在线' }
  return { cls: 'bad', text: '未连接' }
})

const tokenBadge = computed(() => {
  const map = {
    ok: { cls: 'ok', text: '正常' },
    expired: { cls: 'bad', text: '已过期' },
    suspended: { cls: 'warn', text: '已挂起' },
    unbound: { cls: 'warn', text: '未绑定' },
  }
  return map[status.value?.token_status] || { cls: 'warn', text: '-' }
})

async function refresh() {
  try {
    status.value = await apiGet('/status')
    err.value = ''
    updatedAt.value = new Date().toLocaleTimeString()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 5000)  // §6.2：5s 自动刷新
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="card">
    <h2>状态总览
      <small v-if="updatedAt" style="color:#9aa5b1;font-weight:400">
        （{{ updatedAt }} 刷新，每 5 秒自动）
      </small>
    </h2>
    <p v-if="err" class="msg err">{{ err }}</p>
    <table v-if="status">
      <tr>
        <th>连接状态</th>
        <td><span class="badge" :class="connBadge.cls">{{ connBadge.text }}</span></td>
      </tr>
      <tr><th>版本</th><td>{{ status.version }}</td></tr>
      <tr><th>Agent ID</th><td>{{ status.agent_id || '（未绑定）' }}</td></tr>
      <tr><th>活跃任务</th><td>{{ status.active_tasks }}</td></tr>
      <tr><th>在线账号</th><td>{{ (status.accounts || []).length }} 个</td></tr>
      <tr>
        <th>时钟偏差</th>
        <td>{{ (status.clock_offset_seconds ?? 0).toFixed(1) }} 秒
          <span v-if="status.scheduling_paused" class="badge warn">调度已暂停（偏差过大）</span>
        </td>
      </tr>
      <tr>
        <th>令牌状态</th>
        <td>
          <span class="badge" :class="tokenBadge.cls">{{ tokenBadge.text }}</span>
          <span v-if="status.token_expire_at" style="margin-left:8px;color:#7f8c9b">
            到期：{{ status.token_expire_at }}
          </span>
        </td>
      </tr>
      <tr>
        <th>上次断开原因</th>
        <td>{{ status.last_close_reason || '—' }}</td>
      </tr>
    </table>
    <p v-else-if="!err">加载中…</p>
  </div>
</template>
