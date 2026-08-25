<!-- 升级页（S7，§7.4）：GET /upgrade 只读快照（5s 轮询）+
     POST /upgrade/apply（唯一确认入口，走 Nonce 写链路）+ POST /upgrade/snooze。
     failed/rolled_back 展示「下载安装包手动安装」兜底直链（§7.4）。 -->
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { apiGet, apiWrite, UnauthorizedError } from '../api.js'

const info = ref(null)
const err = ref('')
const busy = ref('')

const PHASE_TEXT = {
  noticed: '已发现新版本，准备下载',
  downloading: '安装包下载中',
  ready: '安装包已就绪，可立即升级',
  snoozed: '已暂缓（可随时确认升级）',
  applying: '正在执行升级编排（请勿关机或断电）',
  success: '升级完成',
  failed: '升级失败（见下方救援指引）',
  rolled_back: '升级失败，已自动回滚到旧版本',
}

const phase = computed(() => (info.value && info.value.phase) || null)
const actionable = computed(() => phase.value === 'ready' || phase.value === 'snoozed')
const progress = computed(() => (info.value && info.value.progress) || {})

let timer = null
async function refresh() {
  try {
    info.value = await apiGet('/upgrade')
    err.value = ''
  } catch (e) {
    if (e instanceof UnauthorizedError) return
    err.value = e.message
  }
}

async function apply() {
  busy.value = 'apply'
  try {
    await apiWrite('/upgrade/apply', {})
    await refresh()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  } finally {
    busy.value = ''
  }
}

async function snooze() {
  busy.value = 'snooze'
  try {
    await apiWrite('/upgrade/snooze', {})
    await refresh()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="card">
    <h2>升级</h2>
    <p style="color:#7f8c9b">
      当前版本：<code>{{ info ? info.current_version : '…' }}</code>
    </p>
    <p v-if="err" class="msg err">{{ err }}</p>

    <p v-if="!phase" style="color:#7f8c9b">
      尚未收到升级通知：服务端发布新版本后将自动推送（upgrade_notice）。
    </p>

    <template v-else>
      <table>
        <tr><th>状态</th><td>{{ PHASE_TEXT[phase] || phase }}</td></tr>
        <tr v-if="info.version"><th>目标版本</th><td>{{ info.version }}</td></tr>
        <tr v-if="progress.percent != null && phase === 'downloading'">
          <th>下载进度</th>
          <td>
            {{ progress.percent }}%
            （{{ progress.downloaded_bytes }} / {{ progress.total_bytes || '?' }} 字节）
          </td>
        </tr>
        <tr v-if="info.verified != null && phase === 'ready'">
          <th>哈希校验</th><td>{{ info.verified ? 'SHA-256 通过' : '未校验' }}</td>
        </tr>
        <tr v-if="info.error"><th>错误</th><td class="msg err">{{ info.error }}</td></tr>
        <tr v-if="info.last_rejected">
          <th>最近拒绝的通知</th>
          <td>{{ info.last_rejected.version }}：{{ info.last_rejected.reason }}</td>
        </tr>
      </table>

      <div v-if="actionable" style="margin-top:12px;display:flex;gap:8px">
        <button :disabled="busy !== ''" @click="apply">
          {{ busy === 'apply' ? '提交中…' : '立即安装' }}
        </button>
        <button v-if="phase === 'ready'" class="secondary" :disabled="busy !== ''" @click="snooze">
          {{ busy === 'snooze' ? '提交中…' : '稍后提醒' }}
        </button>
      </div>

      <p v-if="phase === 'applying'" style="color:#b58900">
        编排进行中：停服 → 备份 → 安装 → 启服 → 校验；任一步失败将自动回滚。
      </p>
      <div v-if="(phase === 'failed' || phase === 'rolled_back') && info.download_url">
        <p style="color:#c0392b">
          自动升级未成功，请下载安装包手动安装（§7.4 兜底）：
        </p>
        <p><a :href="info.download_url" target="_blank" rel="noopener">
          下载安装包 sau-{{ info.version }}.exe
        </a></p>
      </div>
    </template>
  </div>
</template>
