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
  let res: Response
  try {
    res = await fetch(path, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch {
    // 断网/跨域等 fetch 层异常：统一友好提示，页面不白屏（T4.10）
    throw new ApiError(-1, '网络连接失败，请检查网络后重试')
  }
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

// ---------- 认证 ----------
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

// ---------- 系统配置（T3.5） ----------
export interface AppConfig {
  tech_indicator_store: boolean
  indicator_cache_days: number
  indicator_engine: string
  report_am_cron: string
  report_pm_cron: string
  llm_model: string
  llm_api_key_masked: string
}

export const configApi = {
  get: () => request<AppConfig>('/api/config/get'),
  save: (payload: Partial<AppConfig> & { llm_api_key?: string }) =>
    request<AppConfig>('/api/config/save', { method: 'POST', body: JSON.stringify(payload) }),
  testLlm: () =>
    request<{ ok: boolean; model?: string; error?: string }>('/api/config/test_llm', {
      method: 'POST',
    }),
}

// ---------- 采集与任务 ----------
export interface TaskItem {
  id: number
  task_name: string
  status: string
  duration_ms: number
  message: string
  created_at: string | null
}

export const collectApi = {
  trigger: (mode: 'full' | 'incremental' | 'single' = 'incremental', dataType?: string) =>
    request<{ task_id: number; status: string; mode: string; data_type: string | null }>(
      '/api/collect/trigger',
      { method: 'POST', body: JSON.stringify({ mode, data_type: dataType ?? null }) },
    ),
}

export const taskApi = {
  latest: (pageSize = 3) =>
    request<{ items: TaskItem[]; total: number; page: number; page_size: number }>(
      `/api/task/status?page=1&page_size=${pageSize}`,
    ),
  list: (params: { keyword?: string; status?: string; page?: number; pageSize?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.keyword) q.set('keyword', params.keyword)
    if (params.status) q.set('status', params.status)
    q.set('page', String(params.page ?? 1))
    q.set('page_size', String(params.pageSize ?? 10))
    return request<{ items: TaskItem[]; total: number; page: number; page_size: number }>(
      `/api/task/status?${q.toString()}`,
    )
  },
}

// ---------- 数据源接口配置（T3.5 / T4.8） ----------
export interface SourceInterface {
  id: number
  data_type: string
  name: string
  impl_kind: string
  impl_ref: string
  params_template: Record<string, unknown>
  role: string
  priority: number
  qps_limit: number
  timeout_sec: number
  enabled: boolean
  last_ok_at: string | null
  last_error: string
}

export interface SourcePayload {
  data_type: string
  name: string
  impl_kind: string
  impl_ref: string
  params_template: Record<string, unknown>
  role: string
  priority: number
  qps_limit: number
  timeout_sec: number
  enabled: boolean
}

export const datasourceApi = {
  list: (dataType?: string) =>
    request<{ items: SourceInterface[]; total: number }>(
      `/api/datasource/interfaces${dataType ? `?data_type=${encodeURIComponent(dataType)}` : ''}`,
    ),
  create: (payload: SourcePayload) =>
    request<SourceInterface>('/api/datasource/interfaces', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  update: (id: number, payload: Partial<SourcePayload>) =>
    request<SourceInterface>(`/api/datasource/interfaces/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  remove: (id: number) =>
    request<{ ok: boolean }>(`/api/datasource/interfaces/${id}`, { method: 'DELETE' }),
  test: (id: number) =>
    request<{ ok: boolean; count: number; duration_ms: number; error?: string }>(
      `/api/datasource/interfaces/${id}/test`,
      { method: 'POST' },
    ),
}

// ---------- 选股器（T5.1 DSL / T5.2 扫描 / T5.3 策略） ----------
export interface SelectorCondition {
  factor: string
  op: string
  value: number
  value2?: number | null
}

export interface StrategyItem {
  id: number
  name: string
  conditions: SelectorCondition[]
  created_at: string | null
}

export interface ScanCandidate {
  code: string
  name: string
  date: string | null
  matched: string[]
  metrics: Record<string, number | null>
  runtime_computed: string[]
}

export interface ScanResult {
  scan_ms: number
  budget_s: number
  conditions: SelectorCondition[]
  trade_date: string | null
  snapshot_source: string
  warnings: string[]
  total: number
  candidates: ScanCandidate[]
}

export const selectorApi = {
  scan: (conditions: SelectorCondition[], tradeDate?: string) =>
    request<ScanResult>('/api/selector/scan', {
      method: 'POST',
      body: JSON.stringify({ conditions, trade_date: tradeDate ?? null }),
    }),
}

export const strategyApi = {
  list: () => request<{ items: StrategyItem[]; total: number }>('/api/selector/strategies'),
  create: (name: string, conditions: SelectorCondition[]) =>
    request<StrategyItem>('/api/selector/strategies', {
      method: 'POST',
      body: JSON.stringify({ name, conditions }),
    }),
  update: (id: number, name: string, conditions: SelectorCondition[]) =>
    request<StrategyItem>(`/api/selector/strategies/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ name, conditions }),
    }),
  remove: (id: number) =>
    request<{ ok: boolean }>(`/api/selector/strategies/${id}`, { method: 'DELETE' }),
}

// ---------- 观察池（T3.4 / T5.4 联动） ----------
export interface WatchItem {
  id: number
  code: string
  name: string
  dimension: string
  created_at: string | null
  // 快照预览（T6.5）
  latest_date?: string | null
  close?: number | null
  pct_change?: number | null
  roe?: number | null
  industry?: string | null
  concepts?: string[] | null
  market_cap?: number | null
}

export const watchlistApi = {
  list: (dimension?: string) =>
    request<{ items: WatchItem[]; total: number; page: number; page_size: number }>(
      `/api/watchlist/list?page=1&page_size=100${dimension ? `&dimension=${encodeURIComponent(dimension)}` : ''}`,
    ),
  add: (code: string, name: string, dimension = '') =>
    request<WatchItem>('/api/watchlist/add', {
      method: 'POST',
      body: JSON.stringify({ code, name, dimension }),
    }),
  remove: (id: number) =>
    request<{ ok: boolean }>(`/api/watchlist/${id}`, { method: 'DELETE' }),
}

// ---------- 回测（T5.6 撮合 / T5.7 统计可视化） ----------
export interface BacktestRunPayload {
  start_date: string
  end_date: string
  initial_capital: number
  strategy_id?: number | null
  fee_rate: number
  slippage: number
  limit_pct?: number
  max_positions?: number
}

export interface BacktestRecord {
  id: number
  status: string // queued / running / success / failed
  strategy_id: number | null
  start_date: string | null
  end_date: string | null
  initial_capital: number
  result: Record<string, any>
  created_at: string | null
}

export const backtestApi = {
  run: (payload: BacktestRunPayload) =>
    request<BacktestRecord>('/api/backtest/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  get: (id: number) => request<BacktestRecord>(`/api/backtest/${id}`),
}

export const healthApi = {
  check: () => request<{ status: string }>('/api/health'),
}
