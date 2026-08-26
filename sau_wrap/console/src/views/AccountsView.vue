<!-- 账号页（S9：登录扫码会话链路 §6.5）
     - 账号列表：GET /accounts/status（双目录兼容扫描；基础判定，真实复核待 /accounts/recheck）；
     - 登录：POST /login/{platform} 创建会话 → 每 2s 轮询 qrcode + status
       （waiting/need_input/success/failed/timeout/cancelled 状态机，§6.5）；
     - need_input：弹验证码输入框 → POST /login/{session_id}/code 注入；
     - 取消：DELETE /login/{session_id}；删除账号：DELETE /accounts（Nonce 防护）。 -->
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { apiGet, apiWrite, apiDelete, fetchQrcodeUrl, UnauthorizedError } from '../api.js'

const PLATFORMS = ['douyin', 'kuaishou', 'xiaohongshu', 'tencent']
const UNSUPPORTED_HINT = {
  bilibili: 'bilibili 登录依赖 biliup 交互式终端，暂不支持服务端扫码（遗留项）',
  baijiahao: 'baijiahao 登录依赖人工调试器交互，暂不支持服务端扫码（遗留项）',
  youtube: 'youtube 登录链路未适配（遗留项）',
}

const accounts = ref([])
const accountsNote = ref('')
const err = ref('')

// ---- 任务 #26：浏览器内核安装态（缺失时展示指引卡）
const browserStatus = ref(null)   // {installed, dir, log, guide, error?}
const browserChecking = ref(false)

async function refreshBrowserStatus() {
  browserChecking.value = true
  try {
    browserStatus.value = await apiGet('/browser/status')
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) browserStatus.value = { installed: false, error: e.message }
  } finally {
    browserChecking.value = false
  }
}

// ---- 登录表单
const platform = ref('douyin')
const accountName = ref('default')
const starting = ref(false)

// ---- 活跃会话
const session = ref(null)      // {session_id, platform, status, message, ...}
const qrcodeUrl = ref('')
const codeInput = ref('')
const submittingCode = ref(false)

let pollTimer = null

const STATUS_TEXT = {
  waiting: '等待扫码…',
  need_input: '需要短信验证码',
  success: '登录成功',
  failed: '登录失败',
  timeout: '登录超时（5 分钟）',
  cancelled: '已取消',
}
const statusText = computed(() =>
  session.value ? (STATUS_TEXT[session.value.status] || session.value.status) : '')
const isTerminal = computed(() =>
  session.value && ['success', 'failed', 'timeout', 'cancelled'].includes(session.value.status))

function badgeClass(status) {
  if (status === 'success') return 'ok'
  if (status === 'failed' || status === 'timeout') return 'bad'
  return ''
}

async function refreshAccounts() {
  try {
    const data = await apiGet('/accounts/status')
    accounts.value = data.accounts || []
    accountsNote.value = data.note || ''
    err.value = ''
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function releaseQrcode() {
  if (qrcodeUrl.value) { URL.revokeObjectURL(qrcodeUrl.value); qrcodeUrl.value = '' }
}

async function pollSession() {
  const s = session.value
  if (!s) return
  try {
    const st = await apiGet(`/login/status/${s.session_id}`)
    session.value = st
    if (st.qrcode_ready) {
      const url = await fetchQrcodeUrl(st.session_id)
      if (url) { releaseQrcode(); qrcodeUrl.value = url }
    }
    if (st.status === 'success') { stopPolling(); refreshAccounts() }
    else if (['failed', 'timeout', 'cancelled'].includes(st.status)) { stopPolling() }
  } catch (e) {
    if (e instanceof UnauthorizedError) stopPolling()
  }
}

async function startLogin() {
  err.value = ''
  if (UNSUPPORTED_HINT[platform.value]) { err.value = UNSUPPORTED_HINT[platform.value]; return }
  starting.value = true
  try {
    const data = await apiWrite(`/login/${platform.value}`, { account_name: accountName.value || 'default' })
    session.value = data
    releaseQrcode()
    stopPolling()
    pollSession()
    pollTimer = setInterval(pollSession, 2000)  // §6.5：前端每 2s 轮询
  } catch (e) {
    if (e instanceof UnauthorizedError) return
    if (e.status === 409 && e.data && e.data.session_id) {
      // 每平台单会话：复用既有会话继续轮询
      session.value = { session_id: e.data.session_id, platform: platform.value, status: 'waiting', message: '' }
      stopPolling()
      pollSession()
      pollTimer = setInterval(pollSession, 2000)
    } else {
      err.value = e.message
    }
  } finally {
    starting.value = false
  }
}

async function submitCode() {
  if (!session.value || !codeInput.value.trim()) return
  submittingCode.value = true
  try {
    await apiWrite(`/login/${session.value.session_id}/code`, { code: codeInput.value.trim() })
    codeInput.value = ''
    pollSession()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  } finally {
    submittingCode.value = false
  }
}

async function cancelSession() {
  if (!session.value) return
  try {
    await apiDelete(`/login/${session.value.session_id}`)
    pollSession()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  }
}

function closeSessionPanel() {
  stopPolling()
  releaseQrcode()
  session.value = null
  codeInput.value = ''
  refreshAccounts()
}

async function removeAccount(acc) {
  if (!window.confirm(`确认删除账号 ${acc.platform_key}_${acc.account_name} 的本地 cookie？`)) return
  try {
    await apiDelete('/accounts', { platform: acc.platform_key, account: acc.account_name })
    refreshAccounts()
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  }
}

onMounted(() => { refreshAccounts(); refreshBrowserStatus() })
onUnmounted(() => { stopPolling(); releaseQrcode() })
</script>

<template>
  <!-- 任务 #26：内核缺失指引卡（命令 + 日志位置 + 完成后刷新重试） -->
  <div v-if="browserStatus && !browserStatus.installed" class="card" style="border-left:4px solid #e6a23c">
    <h2>浏览器内核未安装（登录依赖）</h2>
    <p>登录需要内置浏览器内核，当前未检测到。请在管理员命令行执行：</p>
    <p><code>{{ browserStatus.guide || 'sau.exe browser install' }}</code>
      <span v-if="browserStatus.error" class="msg err" style="margin-left:8px">{{ browserStatus.error }}</span>
    </p>
    <p style="color:#7f8c9b;font-size:12px">
      安装时会自动下载（弱网上限 20 分钟）；进度日志：<code>{{ browserStatus.log || '%ProgramData%\SAU\logs\browser_install.log' }}</code>；
      目标目录：<code>{{ browserStatus.dir || '%ProgramData%\SAU\browsers' }}</code>。
      离线环境可用 <code>sau.exe browser install --from-file &lt;zip&gt;</code>。
    </p>
    <button :disabled="browserChecking" @click="refreshBrowserStatus">
      {{ browserChecking ? '检测中…' : '已安装完成？刷新检测' }}
    </button>
  </div>

  <div class="card">
    <h2>平台账号</h2>
    <p v-if="err" class="msg err">{{ err }}</p>

    <!-- 登录面板 -->
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:16px">
      <div style="min-width:260px">
        <h3 style="font-size:14px">扫码登录</h3>
        <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <select v-model="platform">
            <option v-for="p in PLATFORMS" :key="p" :value="p">{{ p }}</option>
            <option value="bilibili">bilibili（不支持）</option>
            <option value="baijiahao">baijiahao（不支持）</option>
          </select>
          <input v-model="accountName" placeholder="账号名（默认 default）" style="width:150px" />
          <button @click="startLogin" :disabled="starting || (session && !isTerminal)">
            {{ starting ? '启动中…' : '开始登录' }}
          </button>
        </div>
        <p style="color:#7f8c9b;font-size:12px">
          登录在服务进程内以无头浏览器执行；二维码 5 分钟内有效，每平台同时进行一个会话。
          若提示内核未安装，请先执行 <code>sau.exe browser install</code>。
        </p>

        <!-- 活跃会话 -->
        <div v-if="session" style="margin-top:12px">
          <p>
            <b>{{ session.platform }}</b>（{{ session.account_name || 'default' }}）：
            <span class="badge" :class="badgeClass(session.status)">
              {{ statusText }}
            </span>
          </p>
          <p v-if="session.message" style="color:#7f8c9b;font-size:12px">{{ session.message }}</p>
          <img v-if="qrcodeUrl && !isTerminal" :src="qrcodeUrl" alt="登录二维码"
               style="width:220px;height:220px;border:1px solid #e2e6ea;border-radius:6px" />
          <p v-else-if="!isTerminal" style="color:#7f8c9b">二维码加载中…</p>
          <div v-if="session.status === 'need_input'" style="display:flex;gap:8px;margin-top:8px">
            <input v-model="codeInput" placeholder="短信验证码" style="width:140px"
                   @keyup.enter="submitCode" />
            <button @click="submitCode" :disabled="submittingCode">提交验证码</button>
          </div>
          <div style="margin-top:8px;display:flex;gap:8px">
            <button v-if="!isTerminal" @click="cancelSession">取消登录</button>
            <button v-else @click="closeSessionPanel">关闭</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 账号列表 -->
    <table v-if="accounts.length">
      <tr>
        <th>平台</th>
        <th>账号</th>
        <th>状态</th>
        <th>来源</th>
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
        <td>{{ acc.source === 'primary' ? '主目录' : '兼容目录（只读）' }}</td>
        <td>
          <button v-if="acc.source === 'primary'" @click="removeAccount(acc)">删除</button>
          <span v-else style="color:#7f8c9b;font-size:12px">上游目录只读</span>
        </td>
      </tr>
    </table>
    <p v-else style="color:#7f8c9b">
      暂无账号。选择平台后点击「开始登录」完成扫码登录。
    </p>
    <p v-if="accountsNote" style="color:#7f8c9b;font-size:12px">{{ accountsNote }}</p>
  </div>
</template>
