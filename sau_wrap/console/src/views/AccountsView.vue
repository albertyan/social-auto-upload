<!-- 账号页（S9：登录扫码会话链路 §6.5；任务 #3 布局重构）
     - 账号列表：GET /accounts/status（双目录兼容扫描；基础判定，真实复核待 /accounts/recheck）；
     - 多 Tab 按平台分组展示（douyin/kuaishou/xiaohongshu/tencent，前端按 platform_key 过滤；
       四平台之外的账号收纳于「其他」Tab）；
     - 每 Tab「检查状态」→ POST /accounts/recheck（文件级重扫；写请求走 api.js 统一 Nonce 流程）；
     - 「新增平台账号」弹框：POST /login/{platform} 创建会话 → 每 2s 轮询 qrcode + status
       （waiting/need_input/success/failed/timeout/cancelled 状态机，§6.5）；
     - need_input：弹验证码输入框 → POST /login/{session_id}/code 注入；
     - 取消：DELETE /login/{session_id}；删除账号：DELETE /accounts（Nonce 防护）。
     - 任务 #5：登录方式单选（无头默认/有头）；有头模式无二维码、短信二验在浏览器窗口手动完成，
       控制台不渲染注入框（仅引导文案）；503 no_interactive_session 与 failed（含手动命令引导）
       直接展示后端 message。 -->
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { apiGet, apiWrite, apiDelete, fetchQrcodeUrl, UnauthorizedError } from '../api.js'

const PLATFORMS = ['douyin', 'kuaishou', 'xiaohongshu', 'tencent']
const PLATFORM_NAMES = {
  douyin: '抖音',
  kuaishou: '快手',
  xiaohongshu: '小红书',
  tencent: '腾讯视频',
}
const UNSUPPORTED_HINT = {
  bilibili: 'bilibili 登录依赖 biliup 交互式终端，暂不支持服务端扫码（遗留项）',
  baijiahao: 'baijiahao 登录依赖人工调试器交互，暂不支持服务端扫码（遗留项）',
  youtube: 'youtube 登录链路未适配（遗留项）',
}

const accounts = ref([])
const accountsNote = ref('')
const err = ref('')

// ---- 任务 #3：平台 Tab（前端按 platform_key 分组过滤）
const OTHER_TAB = '__other__'  // 兜底 Tab：收纳 platform_key 不属于四平台的账号（如 bilibili/baijiahao/youtube）
const activeTab = ref('douyin')
function isOther(acc) {
  return !PLATFORMS.includes(acc.platform_key)
}
const currentAccounts = computed(() =>
  activeTab.value === OTHER_TAB
    ? accounts.value.filter(isOther)
    : accounts.value.filter((a) => a.platform_key === activeTab.value))
function countOf(platform) {
  if (platform === OTHER_TAB) return accounts.value.filter(isOther).length
  return accounts.value.filter((a) => a.platform_key === platform).length
}
// 「其他」Tab 恒显示（角标可为 0）：与四平台 Tab 行为一致，实现最简

// ---- 任务 #3：检查状态（文件级重扫）提示
const rechecking = ref(false)
const flashNote = ref('')
let flashTimer = null

function showFlash(text) {
  flashNote.value = text
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => { flashNote.value = '' }, 3000)
}

async function recheckAccounts() {
  rechecking.value = true
  try {
    const data = await apiWrite('/accounts/recheck', {})
    accounts.value = data.accounts || []
    err.value = ''
    // 检查口径提示：mode=file_scan，checked_at 为 epoch 秒
    const t = data.checked_at ? new Date(data.checked_at * 1000).toLocaleTimeString() : ''
    accountsNote.value = `文件级检查（${data.mode || 'file_scan'}）完成于 ${t}（cookie 文件有效性，非平台侧实时复核）`
    showFlash('已刷新（文件级检查）')
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) err.value = e.message
  } finally {
    rechecking.value = false
  }
}

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

// ---- 登录表单（任务 #3：挪入「新增平台账号」弹框）
const dialogOpen = ref(false)
const platform = ref('douyin')
const accountName = ref('default')
const loginMode = ref('headless')  // 任务 #5：登录方式（无头默认/有头；请求体附 mode 字段）
const starting = ref(false)

function openDialog() {
  // 「其他」Tab 的账号无法新增登录，弹框平台默认回落到第一个支持平台（下拉仍仅四平台可选）
  platform.value = activeTab.value === OTHER_TAB ? 'douyin' : activeTab.value
  accountName.value = 'default'
  loginMode.value = 'headless'
  dialogOpen.value = true
}

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
// 任务 #5：有头会话——无二维码、不渲染验证码注入框（后端新增 mode 字段，缺省按无头兜底）
const isHeadedSession = computed(() => session.value && session.value.mode === 'headed')

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
    // 任务 #7：陈旧结果防护——请求在途期间会话可能已被关闭/替换，
    // 此时丢弃返回结果（不写回、不续轮询），避免复活已置空的会话
    if (!session.value || session.value.session_id !== s.session_id) return
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
    const data = await apiWrite(`/login/${platform.value}`, {
      account_name: accountName.value || 'default',
      mode: loginMode.value,  // 任务 #5：无头（缺省语义）/有头；后端 400 invalid_mode、503 no_interactive_session
    })
    session.value = data
    releaseQrcode()
    stopPolling()
    pollSession()
    pollTimer = setInterval(pollSession, 2000)  // §6.5：前端每 2s 轮询
  } catch (e) {
    if (e instanceof UnauthorizedError) return
    if (e.status === 409 && e.data && e.data.session_id) {
      // 每平台单会话：复用既有会话继续轮询（mode 未知，以首次轮询返回为准；模板按字段兜底）
      session.value = { session_id: e.data.session_id, platform: platform.value, status: 'waiting', message: '' }
      stopPolling()
      pollSession()
      pollTimer = setInterval(pollSession, 2000)
    } else {
      // 任务 #5：有头链路失败（含 503 no_interactive_session 引导文案）直接展示后端 message，
      // 并提示可经既有「检查状态」入口刷新账号
      err.value = e.message + (loginMode.value === 'headed' ? ' 完成后可点「检查状态」刷新账号。' : '')
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
    await pollSession()
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

// ---- 任务 #3：关闭弹框——活跃会话先走既有取消逻辑，再清理轮询与二维码
async function closeDialog() {
  // 任务 #7：先停轮询，杜绝在途定时器回调与取消流程的竞态；
  // cancelSession 内已 await 收尾轮询，关闭后不再有在途请求写会话状态
  stopPolling()
  if (session.value && !isTerminal.value) {
    await cancelSession()
  }
  closeSessionPanel()
  dialogOpen.value = false
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
onUnmounted(() => { stopPolling(); releaseQrcode(); if (flashTimer) clearTimeout(flashTimer) })
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
    <div class="card-head">
      <h2 style="margin:0">平台账号</h2>
      <div style="display:flex;gap:8px">
        <button @click="openDialog">新增平台账号</button>
        <button class="secondary" @click="refreshAccounts">刷新</button>
      </div>
    </div>
    <p v-if="err" class="msg err">{{ err }}</p>

    <!-- 任务 #3：平台 Tab 条（账号数角标沿用 .badge 风格） -->
    <div class="tabbar">
      <button v-for="p in PLATFORMS" :key="p" class="tab" :class="{ active: activeTab === p }"
              @click="activeTab = p">
        {{ PLATFORM_NAMES[p] }}
        <span class="badge tab-count">{{ countOf(p) }}</span>
      </button>
      <!-- 任务 #7：兜底「其他」Tab，收纳四平台之外的账号（恒显示，角标可为 0） -->
      <button class="tab" :class="{ active: activeTab === OTHER_TAB }" @click="activeTab = OTHER_TAB">
        其他
        <span class="badge tab-count">{{ countOf(OTHER_TAB) }}</span>
      </button>
    </div>

    <!-- 任务 #3：当前 Tab 工具条（检查状态 → POST /accounts/recheck 文件级重扫） -->
    <div class="tab-tools">
      <button class="secondary" :disabled="rechecking" @click="recheckAccounts">
        {{ rechecking ? '检查中…' : '检查状态' }}
      </button>
      <span v-if="flashNote" class="msg ok" style="margin:0">{{ flashNote }}</span>
    </div>

    <!-- 当前 Tab 账号列表 -->
    <table v-if="currentAccounts.length">
      <tr>
        <th v-if="activeTab === OTHER_TAB">平台</th>
        <th>账号</th>
        <th>状态</th>
        <th>来源</th>
        <th>操作</th>
      </tr>
      <tr v-for="acc in currentAccounts" :key="acc.platform_key + ':' + acc.account_name">
        <td v-if="activeTab === OTHER_TAB">{{ acc.platform_key }}</td>
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
      暂无该平台账号。点击「新增平台账号」完成扫码登录。
    </p>
    <p v-if="accountsNote" style="color:#7f8c9b;font-size:12px">{{ accountsNote }}</p>
  </div>

  <!-- 任务 #3：新增平台账号弹框（原生遮罩层；登录会话面板原样搬入） -->
  <div v-if="dialogOpen" class="dialog-mask" @click.self="closeDialog">
    <div class="dialog card">
      <div class="card-head">
        <h2 style="margin:0">新增平台账号</h2>
        <button class="dialog-close" title="关闭" @click="closeDialog">×</button>
      </div>

      <div class="field">
        <label>平台</label>
        <select v-model="platform" :disabled="session && !isTerminal">
          <option v-for="p in PLATFORMS" :key="p" :value="p">{{ PLATFORM_NAMES[p] }}（{{ p }}）</option>
          <option v-for="(hint, p) in UNSUPPORTED_HINT" :key="p" :value="p" disabled :title="hint">
            {{ p }}（不支持）
          </option>
        </select>
        <p v-if="UNSUPPORTED_HINT[platform]" class="msg err">{{ UNSUPPORTED_HINT[platform] }}</p>
      </div>
      <div class="field">
        <label>账号名</label>
        <input v-model="accountName" placeholder="账号名（默认 default）"
               :disabled="session && !isTerminal" @keyup.enter="startLogin" />
      </div>
      <!-- 任务 #5：登录方式单选（无头默认/有头）；会话进行中锁定，与平台/账号名一致 -->
      <div class="field">
        <label>登录方式</label>
        <div class="mode-group">
          <label class="mode-opt">
            <input type="radio" v-model="loginMode" value="headless" :disabled="session && !isTerminal" />
            无头（默认，控制台内扫码）
          </label>
          <label class="mode-opt">
            <input type="radio" v-model="loginMode" value="headed" :disabled="session && !isTerminal" />
            有头（本机桌面浏览器窗口内操作）
          </label>
        </div>
      </div>
      <p style="color:#7f8c9b;font-size:12px">
        无头：登录在服务进程内以无头浏览器执行，二维码 5 分钟内有效；
        有头：在本机桌面打开浏览器窗口，请在窗口中完成登录（含短信验证），无需活跃桌面会话时不可用。
        每平台同时进行一个会话；若提示内核未安装，请先执行 <code>sau.exe browser install</code>。
      </p>
      <button @click="startLogin" :disabled="starting || (session && !isTerminal)">
        {{ starting ? '启动中…' : '开始登录' }}
      </button>

      <!-- 活跃会话（原内联面板搬入弹框，状态机逻辑不变） -->
      <div v-if="session" style="margin-top:12px">
        <p>
          <b>{{ session.platform }}</b>（{{ session.account_name || 'default' }}）：
          <span class="badge" :class="badgeClass(session.status)">
            {{ statusText }}
          </span>
        </p>
        <p v-if="session.message" style="color:#7f8c9b;font-size:12px">{{ session.message }}</p>
        <!-- 任务 #5：有头会话——无二维码区，展示桌面窗口引导文案（含短暂 need_input 也不渲染注入框） -->
        <template v-if="isHeadedSession">
          <p v-if="!isTerminal" class="headed-guide">
            已在本机桌面打开浏览器窗口，请在窗口中完成登录（扫码/输入验证码）。
          </p>
          <p v-else-if="session.status === 'failed'" style="color:#7f8c9b;font-size:12px">
            完成后可点「检查状态」刷新账号。
          </p>
        </template>
        <!-- 无头会话：二维码区 + 验证码注入框（现状不变） -->
        <template v-else>
          <img v-if="qrcodeUrl && !isTerminal" :src="qrcodeUrl" alt="登录二维码"
               style="width:220px;height:220px;border:1px solid #e2e6ea;border-radius:6px" />
          <p v-else-if="!isTerminal" style="color:#7f8c9b">二维码加载中…</p>
          <div v-if="session.status === 'need_input'" style="display:flex;gap:8px;margin-top:8px">
            <input v-model="codeInput" placeholder="短信验证码" style="width:140px"
                   @keyup.enter="submitCode" />
            <button @click="submitCode" :disabled="submittingCode">提交验证码</button>
          </div>
        </template>
        <div style="margin-top:8px;display:flex;gap:8px">
          <button v-if="!isTerminal" @click="cancelSession">取消登录</button>
          <button v-else @click="closeSessionPanel">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
/* 平台 Tab 条（原生实现，视觉对齐页面主色 #3b82f6） */
.tabbar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid #e2e6ea;
  margin-bottom: 12px;
}
.tab {
  background: none;
  color: #556;
  padding: 8px 14px;
  border-radius: 6px 6px 0 0;
  border-bottom: 2px solid transparent;
  font-size: 14px;
}
.tab:hover { background: #f5f6f7; }
.tab.active {
  color: #3b82f6;
  border-bottom-color: #3b82f6;
  font-weight: 600;
}
.tab-count { background: #eef0f2; color: #556; margin-left: 6px; }
.tab.active .tab-count { background: #e8f0fe; color: #3b82f6; }
.tab-tools {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
/* 原生弹框（不引组件库） */
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(31, 45, 61, 0.45);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 8vh 16px 16px;
  z-index: 100;
  overflow-y: auto;
}
.dialog {
  width: 420px;
  max-width: 100%;
  margin-bottom: 0;
}
.dialog select {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #d5dbe3;
  border-radius: 6px;
  font-size: 14px;
  background: #fff;
}
.dialog-close {
  background: none;
  color: #7f8c9b;
  font-size: 20px;
  line-height: 1;
  padding: 4px 8px;
}
.dialog-close:hover { color: #2c3e50; }
/* 任务 #5：登录方式单选（原生 radio，沿用弹框字段风格） */
.mode-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.mode-opt {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: #2c3e50;
  cursor: pointer;
}
.mode-opt input { margin: 0; }
.headed-guide {
  color: #3b82f6;
  background: #e8f0fe;
  border: 1px solid #c6dcfb;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
  margin: 8px 0 0;
}
</style>
