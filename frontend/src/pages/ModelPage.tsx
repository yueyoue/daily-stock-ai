import {
  Alert,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useState } from 'react'
import { ApiError, modelApi } from '../api/client'
import type { ModelCheck, ModelItem, ModelRules, ModelScanResult, ModelVerdict } from '../api/client'

/**
 * 估值模型页（边风炜估值选股）
 *
 * 设计原则：输出「档位 + 区间位置 + 逐条理由」，不是买入名单。
 * 数据来自 /api/model/scan（同步全市场扫描）；规则来自 /api/model/rules。
 */

const VERDICT_META: Record<ModelVerdict, { text: string; color: string }> = {
  pass: { text: '通过', color: 'success' },
  watch: { text: '观察', color: 'warning' },
  reject: { text: '排除', color: 'error' },
}

const STYLE_OPTIONS = [
  { value: '', label: '全部风格' },
  { value: 'bank', label: '银行' },
  { value: 'utility', label: '红利/公用' },
  { value: 'consumer', label: '消费' },
  { value: 'cyclical', label: '周期' },
  { value: 'growth', label: '成长' },
  { value: 'insurance', label: '保险' },
  { value: 'property', label: '地产' },
]

const VERDICT_OPTIONS = [
  { value: '', label: '全部档位' },
  { value: 'pass', label: '通过' },
  { value: 'watch', label: '观察' },
  { value: 'reject', label: '排除' },
]

function fmt(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits)
}

function positionColor(p: number | null): string {
  if (p === null) return '#64748b'
  if (p <= 0.3) return '#22c55e'
  if (p <= 0.6) return '#eab308'
  return '#ef4444'
}

function checkText(c: ModelCheck): string {
  if (c.skipped) return '数据缺失 · 跳过'
  if (c.pass === null) return '—'
  return c.pass ? '满足' : '未满足'
}

export default function ModelPage() {
  const [scan, setScan] = useState<ModelScanResult | null>(null)
  const [rules, setRules] = useState<ModelRules | null>(null)
  const [loading, setLoading] = useState(false)
  const [style, setStyle] = useState('')
  const [verdict, setVerdict] = useState('')
  const [current, setCurrent] = useState<ModelItem | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const payload: { style?: string; verdict?: ModelVerdict; limit: number } = { limit: 300 }
      if (style) payload.style = style
      if (verdict) payload.verdict = verdict as ModelVerdict
      const res = await modelApi.scan(payload)
      setScan(res)
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '扫描失败')
    } finally {
      setLoading(false)
    }
  }, [style, verdict])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    modelApi
      .rules()
      .then(setRules)
      .catch(() => undefined)
  }, [])

  const columns: ColumnsType<ModelItem> = [
    { title: '代码', dataIndex: 'code', width: 92, render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
    { title: '名称', dataIndex: 'name', width: 110 },
    { title: '形态', dataIndex: 'style_label', width: 96, render: (v: string) => <Tag>{v}</Tag> },
    {
      title: '评分',
      dataIndex: 'score',
      width: 88,
      sorter: (a, b) => a.score - b.score,
      defaultSortOrder: 'descend',
      render: (v: number) => <b style={{ color: v >= 75 ? '#4ade80' : v >= 55 ? '#93b4ff' : '#94a3b8' }}>{v}</b>,
    },
    {
      title: '档位',
      dataIndex: 'verdict',
      width: 96,
      render: (v: ModelVerdict) => <Tag color={VERDICT_META[v].color}>{VERDICT_META[v].text}</Tag>,
    },
    {
      title: 'PE(年化)',
      width: 100,
      render: (_, r) => fmt(r.valuation?.pe_annualized as number | null),
    },
    {
      title: 'PB',
      width: 88,
      render: (_, r) => fmt(r.valuation?.pb as number | null),
    },
    {
      title: '股息率',
      width: 92,
      render: (_, r) => {
        const d = r.valuation?.div_yield as number | null | undefined
        return d === null || d === undefined ? '—' : `${d.toFixed(2)}%`
      },
    },
    {
      title: '区间位置',
      width: 132,
      render: (_, r) => (
        <Progress
          percent={r.position === null ? 0 : Math.round(r.position * 100)}
          size="small"
          strokeColor={positionColor(r.position)}
          format={(p) => (r.position === null ? '—' : `${p}%`)}
        />
      ),
    },
    {
      title: '财报可信度',
      dataIndex: 'credibility',
      width: 116,
      render: (v: number, r) => (
        <Space size={4}>
          <span style={{ color: v >= 75 ? '#4ade80' : v >= 60 ? '#fbbf24' : '#f87171' }}>{v}</span>
          {r.red_flags.length > 0 && <Tag color="error">{r.red_flags.length} 红旗</Tag>}
        </Space>
      ),
    },
    { title: '结论', dataIndex: 'decision', ellipsis: true },
  ]

  const stats = scan?.meta.stats

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div>
        <Typography.Title level={4} style={{ margin: 0 }}>
          估值模型 · 边风炜估值选股
        </Typography.Title>
        <Typography.Text type="secondary">
          行业分档 → 口径匹配 → 硬门槛 + 加权评分 → 三档结论与区间位置（研究辅助，不构成投资建议）
        </Typography.Text>
      </div>

      <Row gutter={16}>
        <Col span={6}>
          <Card>
            <Statistic title="全市场扫描" value={stats?.total ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="通过" value={stats?.pass ?? 0} valueStyle={{ color: '#4ade80' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="观察" value={stats?.watch ?? 0} valueStyle={{ color: '#fbbf24' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="排除" value={stats?.reject ?? 0} valueStyle={{ color: '#94a3b8' }} />
          </Card>
        </Col>
      </Row>

      {scan && (
        <Alert
          type="info"
          showIcon
          message={`数据日期 ${scan.meta.trade_date ?? '—'} · PE 口径 ${scan.meta.pe_type} · 无风险利率 ${scan.meta.risk_free}% + 溢价 ${scan.meta.div_premium}%`}
          description={scan.notes.slice(0, 2).join('；')}
        />
      )}

      <Tabs
        items={[
          {
            key: 'scan',
            label: '扫描结果',
            children: (
              <Card
                title="候选列表"
                extra={
                  <Space>
                    <Select options={STYLE_OPTIONS} value={style} onChange={setStyle} style={{ width: 140 }} />
                    <Select options={VERDICT_OPTIONS} value={verdict} onChange={setVerdict} style={{ width: 120 }} />
                    <Typography.Text type="secondary">
                      {scan?.meta.filtered ?? 0} 条
                    </Typography.Text>
                  </Space>
                }
              >
                <Table<ModelItem>
                  rowKey="code"
                  size="small"
                  loading={loading}
                  dataSource={scan?.items ?? []}
                  columns={columns}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  onRow={(record) => ({ onClick: () => setCurrent(record), style: { cursor: 'pointer' } })}
                />
              </Card>
            ),
          },
          {
            key: 'rules',
            label: '规则配置',
            children: rules ? (
              <Row gutter={[16, 16]}>
                {rules.styles.map((s) => (
                  <Col xs={24} md={12} xl={8} key={s.style}>
                    <Card
                      title={
                        <Space>
                          {s.label}
                          {s.degraded && <Tag color="warning">口径降级</Tag>}
                          {s.observe_only && <Tag>仅观察</Tag>}
                        </Space>
                      }
                      size="small"
                    >
                      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                        {s.note}
                      </Typography.Paragraph>
                      <Descriptions column={1} size="small">
                        {s.hard.length === 0 && (
                          <Descriptions.Item label="硬门槛">无（不参与评分）</Descriptions.Item>
                        )}
                        {s.hard.map(([factor, op, target]) => (
                          <Descriptions.Item key={`${s.style}-${factor}`} label={factor}>
                            {op === 'between'
                              ? `${(target as [number, number])[0]} ~ ${(target as [number, number])[1]}`
                              : `${op} ${target}`}
                          </Descriptions.Item>
                        ))}
                        <Descriptions.Item label="区间">
                          {s.band[0]} {s.band[1]} ~ {s.band[2]}
                        </Descriptions.Item>
                      </Descriptions>
                    </Card>
                  </Col>
                ))}
                <Col span={24}>
                  <Card size="small" title="全局参数">
                    <Descriptions column={4} size="small">
                      <Descriptions.Item label="权重">
                        {Object.entries(rules.weights).map(([k, v]) => `${k}:${v}`).join(' / ')}
                      </Descriptions.Item>
                      <Descriptions.Item label="无风险利率">{rules.risk_free}%</Descriptions.Item>
                      <Descriptions.Item label="股息率溢价">+{rules.div_premium}%</Descriptions.Item>
                      <Descriptions.Item label="gate 阈值">
                        {rules.gate.watch_below} / {rules.gate.reject_below}
                      </Descriptions.Item>
                    </Descriptions>
                  </Card>
                </Col>
              </Row>
            ) : (
              <Empty description="规则加载中" />
            ),
          },
        ]}
      />

      <Drawer
        width={640}
        open={!!current}
        onClose={() => setCurrent(null)}
        title={current ? `${current.name}（${current.code}）· ${current.style_label}` : ''}
      >
        {current ? (
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            <Space size={12} wrap>
              <Tag color={VERDICT_META[current.verdict].color}>
                {VERDICT_META[current.verdict].text}
              </Tag>
              <Typography.Text>综合 {current.score} 分</Typography.Text>
              <Typography.Text type="secondary">财报可信度 {current.credibility}</Typography.Text>
            </Space>

            <Alert type={current.verdict === 'reject' ? 'error' : 'info'} showIcon message={current.decision} />

            <div>
              <Typography.Text type="secondary">
                估值区间：{current.band.factor} {current.band.low} ~ {current.band.high}
              </Typography.Text>
              <Progress
                percent={current.position === null ? 0 : Math.round(current.position * 100)}
                strokeColor={positionColor(current.position)}
                format={() => (current.position === null ? '无数据' : `${Math.round(current.position * 100)}%`)}
              />
            </div>

            <div>
              <Typography.Text strong>规则命中</Typography.Text>
              <Table<ModelCheck>
                rowKey={(c) => `${c.factor}-${c.op}`}
                size="small"
                pagination={false}
                style={{ marginTop: 8 }}
                dataSource={current.checks}
                columns={[
                  { title: '因子', dataIndex: 'factor', width: 130 },
                  {
                    title: '门槛',
                    width: 150,
                    render: (_, c) =>
                      c.op === 'between'
                        ? `${(c.target as [number, number])[0]} ~ ${(c.target as [number, number])[1]}`
                        : `${c.op} ${c.target}`,
                  },
                  { title: '实际', width: 100, render: (_, c) => fmt(c.value, 2) },
                  { title: '结果', render: (_, c) => checkText(c) },
                ]}
              />
            </div>

            {current.reasons.length > 0 && (
              <div>
                <Typography.Text strong>模型理由</Typography.Text>
                <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
                  {current.reasons.map((r) => (
                    <li key={r}>
                      <Typography.Text type="secondary">{r}</Typography.Text>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {current.red_flags.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="财报红旗"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {current.red_flags.map((f) => (
                      <li key={f}>{f}</li>
                    ))}
                  </ul>
                }
              />
            )}

            {current.rules_note && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                口径说明：{current.rules_note}
              </Typography.Text>
            )}
          </Space>
        ) : null}
      </Drawer>
    </Space>
  )
}