import {
  Button,
  Card,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'
import { useCallback, useEffect, useRef, useState } from 'react'
import { collectApi, taskApi } from '../api/client'
import type { TaskItem } from '../api/client'

/** 任务监控（T4.6，原型"任务监控"页）：task_log 表格 + 手动触发 + 失败原因展开 */

const STATUS_TONE: Record<string, { color: string; label: string }> = {
  queued: { color: 'default', label: '排队中' },
  running: { color: 'processing', label: '运行中' },
  success: { color: 'success', label: '成功' },
  failed: { color: 'error', label: '失败' },
warning: { color: 'warning', label: '告警' },
  // D-10 修复：任务被跳过的终态（如非交易日跳过报告生成）此前未登记，会显示为原始英文
  skipped: { color: 'default', label: '已跳过' },
}

type CollectMode = 'incremental' | 'full' | 'single'

const MODE_OPTIONS: { value: CollectMode; label: string }[] = [
  { value: 'incremental', label: '日更增量采集' },
  { value: 'full', label: '全量采集' },
  { value: 'single', label: '单数据类型采集' },
]

const DATA_TYPE_OPTIONS = [
  { value: 'stock_info', label: '股票基础信息' },
  { value: 'kline', label: '日线K线' },
  { value: 'finance', label: '财务数据' },
  { value: 'market_daily', label: '大盘汇总' },
  { value: 'moneyflow', label: '资金流向' },
  { value: 'limit_updown', label: '涨跌停统计' },
  { value: 'market_summary', label: '涨跌家数汇总' },
]

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fmtDuration(ms: number): string {
  if (!ms && ms !== 0) return '—'
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}秒`
  return `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`
}

export default function TaskPage() {
  const [items, setItems] = useState<TaskItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState<string | undefined>()
  const [loading, setLoading] = useState(false)
  const [mode, setMode] = useState<CollectMode>('incremental')
  const [dataType, setDataType] = useState<string>('kline')
  const [triggering, setTriggering] = useState(false)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const p = await taskApi.list({ keyword, status, page, pageSize })
      setItems(p.items)
      setTotal(p.total)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '任务日志加载失败')
    } finally {
      setLoading(false)
    }
  }, [keyword, status, page, pageSize])

  useEffect(() => {
    void load()
  }, [load])

  // 自动轮询：触发采集后无需手动刷新即可看到进度
  useEffect(() => {
    timer.current = setInterval(() => {
      void taskApi
        .list({ keyword, status, page, pageSize })
        .then((p) => {
          setItems(p.items)
          setTotal(p.total)
        })
        .catch(() => undefined)
    }, 10000)
    return () => {
      if (timer.current) clearInterval(timer.current)
    }
  }, [keyword, status, page, pageSize])

  const trigger = async () => {
    setTriggering(true)
    try {
      const r = await collectApi.trigger(mode, mode === 'single' ? dataType : undefined)
      message.success(`采集任务已触发（task_id=${r.task_id}）`)
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '触发失败')
    } finally {
      setTriggering(false)
    }
  }

  const columns: ColumnsType<TaskItem> = [
    {
      title: '任务名称',
      dataIndex: 'task_name',
      key: 'task_name',
      ellipsis: true,
    },
    {
      title: '执行时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (v: string | null) => fmtTime(v),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v: string) => {
        const tone = STATUS_TONE[v] ?? { color: 'default', label: v }
        return <Tag color={tone.color}>{tone.label}</Tag>
      },
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      width: 110,
      render: (v: number) => fmtDuration(v),
    },
    {
      title: '详情',
      dataIndex: 'message',
      key: 'message',
      ellipsis: true,
      render: (v: string) => v || '—',
    },
  ]

  const expandable: TableProps<TaskItem>['expandable'] = {
    rowExpandable: (r) => Boolean(r.message),
    expandedRowRender: (r) => (
      <Typography.Paragraph
        style={{ whiteSpace: 'pre-wrap', marginBottom: 0, fontFamily: 'monospace' }}
        copyable={Boolean(r.message)}
      >
        {r.message}
      </Typography.Paragraph>
    ),
  }

  return (
    <div>
      <Typography.Title level={2} style={{ marginTop: 0 }}>
        任务监控
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        采集、指标计算、报告生成任务日志与状态（失败原因可展开查看）
      </Typography.Paragraph>

      <Card style={{ marginBottom: 16 }}>
        <Space wrap>
          <Select
            value={mode}
            options={MODE_OPTIONS}
            style={{ width: 170 }}
            onChange={(v) => setMode(v)}
          />
          {mode === 'single' && (
            <Select
              value={dataType}
              options={DATA_TYPE_OPTIONS}
              style={{ width: 170 }}
              onChange={(v) => setDataType(v)}
            />
          )}
          <Button type="primary" loading={triggering} onClick={() => void trigger()}>
            手动触发采集
          </Button>
          <Input.Search
            allowClear
            placeholder="任务名 / 消息关键词"
            style={{ width: 220 }}
            onSearch={(v) => {
              setPage(1)
              setKeyword(v.trim())
            }}
          />
          <Select
            allowClear
            placeholder="状态筛选"
            style={{ width: 130 }}
            value={status}
            options={[
              { value: 'running', label: '运行中' },
              { value: 'success', label: '成功' },
              { value: 'failed', label: '失败' },
              { value: 'warning', label: '告警' },
              // D-10 修复：补充 skipped 筛选项（与后端 status 白名单一致）
              { value: 'skipped', label: '已跳过' },
            ]}
            onChange={(v) => {
              setPage(1)
              setStatus(v)
            }}
          />
          <Button onClick={() => void load()}>刷新</Button>
        </Space>
      </Card>

      <Card>
        <Table<TaskItem>
          rowKey="id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={items}
          expandable={expandable}
          scroll={{ x: 800 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => {
              setPage(p)
              setPageSize(ps)
            },
          }}
        />
      </Card>
    </div>
  )
}
