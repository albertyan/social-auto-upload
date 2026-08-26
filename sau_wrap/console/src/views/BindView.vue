<!-- 绑定页：机器码展示 + GET/POST /config + POST /bind + POST /reload（§6.2）
     任务 #26：已绑定态仍可编辑 Server URL / Agent Token 并重新提交（重配
     token 入口，走既有 POST /bind，Nonce+审计已具备） -->
<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, apiWrite, UnauthorizedError } from '../api.js'

const machineCode = ref('')
const machineErr = ref('')
const config = ref(null)
const form = ref({ server_url: '', token: '', agent_id: '' })
const msg = ref('')
const msgOk = ref(true)
const busy = ref(false)

async function loadAll() {
  try {
    const mc = await apiGet('/machine-code')
    machineCode.value = mc.machine_code
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) machineErr.value = e.message
  }
  try {
    config.value = await apiGet('/config')
    if (config.value.bound) form.value.server_url = config.value.server_url || ''
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) showErr(e.message)
  }
}

function showMsg(text) { msg.value = text; msgOk.value = true }
function showErr(text) { msg.value = text; msgOk.value = false }

async function doBind() {
  busy.value = true
  msg.value = ''
  try {
    const r = await apiWrite('/bind', {
      server_url: form.value.server_url,
      token: form.value.token,
      agent_id: form.value.agent_id || undefined,
    })
    showMsg(`绑定成功：agent_id=${r.agent_id}；${r.reload || '热重载已触发'}`)
    form.value.token = ''
    await loadAll()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) showErr(`绑定失败：${e.message}`)
  } finally {
    busy.value = false
  }
}

async function doSaveConfig() {
  busy.value = true
  msg.value = ''
  try {
    const r = await apiWrite('/config', { server_url: form.value.server_url })
    showMsg(`已保存并热重载：${r.reload || ''}`)
    await loadAll()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) showErr(`保存失败：${e.message}`)
  } finally {
    busy.value = false
  }
}

async function doReload() {
  busy.value = true
  msg.value = ''
  try {
    const r = await apiWrite('/reload', {})
    showMsg(`热重载已触发：${r.reload || ''}`)
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) showErr(`热重载失败：${e.message}`)
  } finally {
    busy.value = false
  }
}

function copyCode() {
  navigator.clipboard?.writeText(machineCode.value)
    .then(() => showMsg('机器码已复制'))
    .catch(() => showErr('复制失败，请手动选择复制'))
}

onMounted(loadAll)
</script>

<template>
  <div class="card">
    <h2>机器码（提供给 opcgeo 后台生成绑定凭证）</h2>
    <p v-if="machineErr" class="msg err">{{ machineErr }}</p>
    <p v-else style="font-family:Consolas,monospace;word-break:break-all">
      {{ machineCode || '加载中…' }}
      <button v-if="machineCode" class="secondary" style="margin-left:8px" @click="copyCode">
        复制
      </button>
    </p>
  </div>

  <div class="card">
    <h2>绑定配置</h2>
    <p v-if="config && !config.bound" class="msg err">
      尚未绑定：请填写 Server URL 与 Token 后点击「绑定」。
    </p>
    <p v-else-if="config" class="msg ok">
      已绑定：{{ config.server_url }}（agent_id={{ config.agent_id }}）
    </p>
    <div class="field">
      <label>Server URL（ws:// 或 wss://）</label>
      <input v-model="form.server_url" placeholder="wss://example.com/ws" />
    </div>
    <!-- 任务 #26：Token / Agent ID 在已绑定态同样可编辑（重置 token 后无需命令行兜底） -->
    <div class="field">
      <label>Token（opcgeo 后台签发；已绑定时可填新 token 重新提交）</label>
      <input v-model="form.token" type="password" placeholder="绑定令牌" />
    </div>
    <div class="field">
      <label>Agent ID（可选，后台已分配时填写）</label>
      <input v-model="form.agent_id" placeholder="可留空" />
    </div>
    <button :disabled="busy || !form.token" @click="doBind">
      {{ config && config.bound ? '重新配置并绑定' : '绑定' }}
    </button>
    <template v-if="config && config.bound">
      <button class="secondary" style="margin-left:8px" :disabled="busy" @click="doSaveConfig">
        仅改地址并热重载
      </button>
      <button class="secondary" style="margin-left:8px" :disabled="busy" @click="doReload">
        手动热重载
      </button>
    </template>
    <p v-if="msg" class="msg" :class="msgOk ? 'ok' : 'err'">{{ msg }}</p>
  </div>
</template>
