// API 客户端封装：同源 Cookie 会话、统一错误格式 {code,message}、401 抛出（由路由守卫跳登录）
export class ApiError extends Error {
  code: number
  constructor(code: number, message: string) {
    super(message)
    this.code = code
    this.name = 'ApiError'
  }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  let data: { code?: number; message?: string } = {}
  try {
    data = await res.json()
  } catch {
    /* 空响应体 */
  }
  if (!res.ok) {
    throw new ApiError(data.code ?? res.status, data.message ?? `请求失败(${res.status})`)
  }
  return data as T
}

export interface SessionUser {
  username: string
}

export const authApi = {
  login: (username: string, password: string) =>
    request<SessionUser>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  session: () => request<SessionUser>('/api/auth/session'),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
}

export const healthApi = {
  check: () => request<{ status: string }>('/api/health'),
}
