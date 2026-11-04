import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Col,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useState } from 'react'
import { ApiError, selectorApi, strategyApi, watchlistApi } from '../api/client'
import type { ScanCandidate, ScanResult, SelectorCondition, StrategyItem } from '../api/client'

/** 选股器（T5.2 扫描 / T5.3 策略侧栏 / T5.4 结果表） */

// 因子清单与 T2.1/T2.2 INDICATOR_SPEC 对齐（服务端 DSL 校验为准）
const TECH_FACTORS = [
  'ma5', 'ma10', 'ma20', 'ma60', 'ema12', 'ema26', 'macd_dif', 'macd_dea', 'macd_hist',
  'kdj_k', 'kdj_d', 'kdj_j', 'rsi6', 'rsi14', 'boll_upper', 'boll_mid', 'boll_lower',
  'wr14', 'cci14', 'bias6', 'atr14', 'obv', 'roc12', 'mtm12', 'dma_dif', 'dma_ama', 'vr26', 'psy12',
]
const FIN_FACTORS = [
  'total_revenue', 'net_profit', 'deduct_net_profit', 'operating_cashflow', 'invest_cashflow',
  'finance_cashflow', 'total_assets', 'total_liabilities', 'equity', 'eps', 'bvps', 'ocf_ps',
  'gross_margin', 'net_margin', 'operating_margin', 'roe', 'roa', 'debt_to_asset', 'equity_ratio',
  'current_ratio', 'quick_ratio', 'ocf_to_profit', 'ocf_to_revenue', 'deduct_ratio',
  'rd_expense_ratio', 'revenue_yoy', 'profit_yoy', 'ocf_yoy', 'revenue_qoq', 'profit_qoq',
]

const FACTOR_OPTIONS = [
  { label: '技术因子（28）', options: TECH_FACTORS.map((f) => ({ value: f })) },
  { label: '财务因子（30）', options: FIN_FACTORS.map((f) => ({ value: f })) },
]

const OP_OPTIONS = ['>', '>=', '<', '<=', '==', '!=', 'between'].map((v) => ({ value: v, label: v }))

interface CondRow {
  factor: string
  op: string
  value: number | null
  value2: number | null
}

const newRow = (): CondRow => ({ factor: 'ma5', op: '>', value: null, value2: null })

export default function SelectorPage() {
  const [rows, setRows] = useState<CondRow[]>([newRow()])
  const [tradeDate, setTradeDate] = useState('')
  const [scanning, setScanning] = useState(false)
  const [result, setResult] = useState<ScanResult | null>(null)

  const [strategies, setStrategies] = useState<StrategyItem[]>([])
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editingName, setEditingName] = useState('')
  const [saveOpen, setSaveOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [addedCodes, setAddedCodes] = useState<string[]>([])

  const loadStrategies = useCallback(async () => {
    try {
      const p = await strategyApi.list()
      setStrategies(p.items)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '策略列表加载失败')
    }
  }, [])

  useEffect(() => {
    void loadStrategies()
  }, [loadStrategies])

  const buildConditions = (): SelectorCondition[] | null => {
    const out: SelectorCondition[] = []
    for (const r of rows) {
      if (!r.factor.trim() || r.value === null) {
        message.warning('条件需填因子与阈值')
        return null
      }
      if (r.op === 'between' && r.value2 === null) {
        message.warning('between 需要区间上界')
        return null
      }
      const c: SelectorCondition = { factor: r.factor.trim(), op: r.op, value: r.value }
      if (r.op === 'between') c.value2 = r.value2
      out.push(c)
    }
    if (!out.length) {
      message.warning('至少一条条件')
      return null
    }
    return out
  }

  const scan = async (conditions?: SelectorCondition[]) => {
    const conds = conditions ?? buildConditions()
    if (!conds) return
    setScanning(true)
    try {
      const r = await selectorApi.scan(conds, tradeDate.trim() || undefined)
      setResult(r)
      message.success(`扫描完成：${r.total} 只候选，耗时 ${r.scan_ms}ms`)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '扫描失败')
    } finally {
      setScanning(false)
    }
  }

  const saveStrategy = async () => {
    const conds = buildConditions()
    if (!conds) return
    if (!editingName.trim()) {
      message.warning('请填写策略名')
      return
    }
    setSaving(true)
    try {
      if (editingId) {
        await strategyApi.update(editingId, editingName.trim(), conds)
        message.success('策略已更新')
      } else {
        await strategyApi.create(editingName.trim(), conds)
        message.success('策略已保存')
      }
      setSaveOpen(false)
      await loadStrategies()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const loadStrategy = (s: StrategyItem) => {
    setRows(s.conditions.map((c) => ({
      factor: c.factor,
      op: c.op,
      value: c.value,
      value2: c.value2 ?? null,
    })))
    setEditingId(s.id)
    setEditingName(s.name)
    message.success(`已加载策略「${s.name}」，可直接扫描或编辑后更新`)
  }

  const removeStrategy = async (s: StrategyItem) => {
    try {
      await strategyApi.remove(s.id)
      if (editingId === s.id) setEditingId(null)
      message.success('策略已删除')
      await loadStrategies()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  const addToWatch = async (c: ScanCandidate) => {
    try {
      await watchlistApi.add(c.code, c.name, editingName || '选股命中')
      setAddedCodes((prev) => [...prev, c.code])
      message.success(`${c.code} ${c.name} 已加入观察池`)
    } catch (e) {
      if (e instanceof ApiError && e.code === 409) {
        setAddedCodes((prev) => [...prev, c.code])
        message.info(`${c.code} 已在观察池中`)
      } else {
        message.error(e instanceof Error ? e.message : '加入观察池失败')
      }
    }
  }

  const columns: ColumnsType<ScanCandidate> = [
    { title: '代码', dataIndex: 'code', key: 'code', width: 100 },
    {
      title: '名称',
      key: 'name',
      width: 100,
      render: (_, r) => r.name || '—',
    },
    {
      title: '命中条件',
      key: 'matched',
      render: (_, r) => (
        <Space size={4} wrap>
          {r.matched.map((m) => (
            <Tag key={m} color="blue">{m}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '指标值',
      key: 'metrics',
      render: (_, r) => {
        const condFactors = new Set((result?.conditions ?? []).map((c) => c.factor))
        return (
          <Space size={4} wrap>
            {Object.entries(r.metrics).map(([k, v]) => (
              <Tag key={k} color={condFactors.has(k) ? 'green' : undefined}>
                {k}={v === null || v === undefined ? '—' : Number(v).toFixed(3)}
              </Tag>
            ))}
          </Space>
        )
      },
    },
    {
      title: '实时兜底',
      key: 'runtime_computed',
      width: 110,
      render: (_, r) =>
        r.runtime_computed.length ? (
          <Space size={2} wrap>
            {r.runtime_computed.map((f) => (
              <Tag key={f} color="orange">{f}</Tag>
            ))}
          </Space>
        ) : (
          '—'
        ),
    },
    { title: '交易日', dataIndex: 'date', key: 'date', width: 110 },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, r) =>
        addedCodes.includes(r.code) ? (
          <Tag color="green">已入池</Tag>
        ) : (
          <Button size="small" onClick={() => void addToWatch(r)}>
            加入观察池
          </Button>
        ),
    },
  ]

  return (
    <div>
      <Typography.Title level={2} style={{ marginTop: 0 }}>
        选股器
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        财务/技术因子 × 比较符 × 阈值/区间，AND 组合；扫描走指标缓存（不联网），20 秒预算
      </Typography.Paragraph>

      <Row gutter={[16, 16]}>
        {/* 策略侧栏（T5.3） */}
        <Col xs={24} lg={6}>
          <Card
            title="我的策略"
            extra={
              <Button size="small" onClick={() => { setEditingId(null); setEditingName(''); setSaveOpen(true) }}>
                保存当前
              </Button>
            }
          >
            <List
              size="small"
              dataSource={strategies}
              locale={{ emptyText: '暂无保存的策略' }}
              renderItem={(s) => (
                <List.Item
                  actions={[
                    <Button key="load" size="small" type="link" onClick={() => loadStrategy(s)}>
                      加载
                    </Button>,
                    <Popconfirm key="del" title="确认删除该策略？" onConfirm={() => void removeStrategy(s)}>
                      <Button size="small" type="link" danger>
                        删除
                      </Button>
                    </Popconfirm>,
                  ]}
                >
                  <List.Item.Meta
                    title={s.name}
                    description={`${s.conditions.length} 条件`}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>

        {/* 条件编辑 + 结果 */}
        <Col xs={24} lg={18}>
          <Card
            title={editingId ? `选股条件（AND）· 正在编辑「${editingName}」` : '选股条件（AND）'}
            extra={
              <Space wrap>
                <Input
                  placeholder="交易日（可选 yyyy-mm-dd）"
                  style={{ width: 180 }}
                  value={tradeDate}
                  onChange={(e) => setTradeDate(e.target.value)}
                />
                <Button onClick={() => setRows([...rows, newRow()])}>添加条件</Button>
                <Button type="primary" loading={scanning} onClick={() => void scan()}>
                  扫描全市场
                </Button>
              </Space>
            }
          >
            {rows.map((r, i) => (
              <Space key={i} style={{ display: 'flex', marginBottom: 8 }} wrap>
                <AutoComplete
                  style={{ width: 200 }}
                  value={r.factor}
                  options={FACTOR_OPTIONS}
                  placeholder="因子（可输入自定义如 ma30）"
                  onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, factor: v } : x)))}
                />
                <Select
                  style={{ width: 90 }}
                  value={r.op}
                  options={OP_OPTIONS}
                  onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, op: v } : x)))}
                />
                <InputNumber
                  placeholder={r.op === 'between' ? '下界' : '阈值'}
                  value={r.value}
                  onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, value: v } : x)))}
                />
                {r.op === 'between' && (
                  <InputNumber
                    placeholder="上界"
                    value={r.value2}
                    onChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, value2: v } : x)))}
                  />
                )}
                {rows.length > 1 && (
                  <Button danger size="small" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                    删行
                  </Button>
                )}
              </Space>
            ))}
          </Card>

          {result && (
            <Card style={{ marginTop: 16 }} title={`扫描结果（${result.total} 只）`}>
              {result.warnings.map((w) => (
                <Alert key={w} type="warning" showIcon message={w} style={{ marginBottom: 8 }} />
              ))}
              <Table<ScanCandidate>
                rowKey="code"
                size="small"
                columns={columns}
                dataSource={result.candidates}
                scroll={{ x: 800 }}
                pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 只` }}
              />
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
                扫描耗时 {result.scan_ms}ms（预算 {result.budget_s}s）· 快照来源 {result.snapshot_source}
                {' · ⚠️ 回测/选股结果不代表未来收益'}
              </Typography.Paragraph>
            </Card>
          )}
        </Col>
      </Row>

      <Modal
        title={editingId ? '更新策略' : '保存为策略'}
        open={saveOpen}
        onCancel={() => setSaveOpen(false)}
        onOk={() => void saveStrategy()}
        confirmLoading={saving}
      >
        <Input
          placeholder="策略名，如 稳健成长"
          value={editingName}
          onChange={(e) => setEditingName(e.target.value)}
        />
      </Modal>
    </div>
  )
}
