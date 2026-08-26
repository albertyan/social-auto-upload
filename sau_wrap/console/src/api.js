// API 封装（设计文档 §6.3）：
// - 同源请求带 Cookie（票据核销后服务端种的会话）；
// - 统一 401 拦截 → 跳转引导页「会话已失效，请从托盘重新打开控制台」，
//   且禁止失效态发起写操作（抛错中断调用方流程）；
// - 写操作先 GET /nonce 领取一次性 Nonce，经 X-Console-Nonce 头提交（§6.3 定案）。
import { router } from './router.js'

export class UnauthorizedError extends Error {
  constructor() {
    super('unauthorized')
  }
}

function redirectToGuide() {
  if (router.currentRoute.value.name !== 'unauthorized') {
    router.push({ name: 'unauthorized' })
  }
}

async function request(path, { method = 'GET', body, nonce } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (nonce) headers['X-Console-Nonce'] = nonce
  const resp = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  })
  if (resp.status === 401) {
    redirectToGuide()
    throw new UnauthorizedError()
  }
  const text = await resp.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!resp.ok) {
    const err = new Error((data && (data.message || data.error)) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.data = data
    throw err
  }
  return data
}

export function apiGet(path) {
  return request(path)
}

/** 写操作：先领一次性 Nonce 再提交（§6.3：服务端一次性消费、短窗去重）。 */
export async function apiWrite(path, body) {
  const { nonce } = await request('/nonce')
  return request(path, { method: 'POST', body, nonce })
}

/** DELETE 写操作（S9：账号删除 / 登录会话取消）：同样先领 Nonce。 */
export async function apiDelete(path, body) {
  const { nonce } = await request('/nonce')
  return request(path, { method: 'DELETE', body, nonce })
}

/** 拉取登录二维码（S9）：返回 blob URL（可直接作 <img src>）；未就绪返回 null。
 *  调用方负责用完后 URL.revokeObjectURL 释放。 */
export async function fetchQrcodeUrl(sessionId) {
  const resp = await fetch(`/login/qrcode/${encodeURIComponent(sessionId)}`, {
    credentials: 'same-origin',
  })
  if (resp.status === 401) {
    redirectToGuide()
    throw new UnauthorizedError()
  }
  if (!resp.ok) return null
  const blob = await resp.blob()
  return URL.createObjectURL(blob)
}
