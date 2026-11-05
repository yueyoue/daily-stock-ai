import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Form,
  InputNumber,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, backtestApi, strategyApi } from '../api/client'
import type { BacktestRecord, StrategyItem } from '../api/client'
import EChart from '../components/EChart'

/** 策略回测（T5.7 统计与可视化；撮合引擎 T5.6 / 统计口径 T5.7 后端） */

interface TradeRow {
  code: string
  side: string
  date: string
  signal_date?: string
  price: number
  quantity: number
  fee: number
  pnl?: number
  reason?: string
}

const RISK_NOTICE = '回测基于历史数据模拟，不代表未来收益'

const pct = (v: number | null | undefined, digits = 1) =>
  v == null ? '—' : `${(v * 100).toFixed(digits)}%`

export default function BacktestPage() {
  const [form] = Form.useForm()
  const [strategies, setStrategies] = useState<StrategyItem[]>([])
  const [running, setRunning] = useState(false)
  const [record, setRecord] = useState<BacktestRecord | null>(null)
  const pollRef = useRef(false)

  const loadStrategies = useCallback(async () => {
    try {
      const r = await strategyApi.list()
      setStrategies(r.items)
    } catch (e) {
      if (e instanceof ApiError) message.error(e.message)
    }
  }, [])

  useEffect(() => {
    loadStrategies()
    return () => {
      pollRef.current = false
    }
  }, [loadStrategies])

  const poll = async (id: number) => {
    pollRef.current = true
    for (let i = 0; i < 180 && pollRef.current; i++) {
      await new Promise((r) => setTimeout(r, 2000))
      try {
        const rec = await backtestApi.get(id)
        setRecord(rec)
        if (rec.status === 'success' || rec.status === 'failed') {
          setRunning(false)
          if (rec.status === 'failed') message.error(rec.result?.error || '回测失败')
          return
        }
      } catch (e) {
        setRunning(false)
        if (e instanceof ApiError) message.error(e.message)
        return
      }
    }
    setRunning(false)
    message.warning('回测轮询超时，请稍后在任务监控页查看')
  }

  const onFinish = async (values: {
    range: [Dayjs, Dayjs]
    initial_capital: number
    strategy_id: number
    fee_rate: number
    slippage: number
    limit_pct: number
    max_positions: number
  }) => {
    setRunning(true)
    setRecord(null)
    try {
      const rec = await backtestApi.run({
        start_date: values.range[0].format('YYYY-MM-DD'),
        end_date: values.range[1].format('YYYY-MM-DD'),
        initial_capital: values.initial_capital,
        strategy_id: values.strategy_id,
        fee_rate: values.fee_rate,
        slippage: values.slippage,
        limit_pct: values.limit_pct,
        max_positions: values.max_positions,
      })
      setRecord(rec)
      poll(rec.id)
    } catch (e) {
      setRunning(false)
      if (e instanceof ApiError) message.error(e.message)
    }
  }

  const result = record?.result ?? {}
  const curve: { date: string; equity: number }[] = result.equity_curve ?? []
  const trades: TradeRow[] = (result.trade_details ?? result.trades ?? []).map(
    (t: TradeRow, i: number) => ({ ...t, key: i }),
  )

  const columns: ColumnsType<TradeRow> = [
    { title: '成交日', dataIndex: 'date', key: 'date' },
    { title: '信号日', dataIndex: 'signal_date', key: 'signal_date' },
    { title: '代码', dataIndex: 'code', key: 'code' },
    {
      title: '方向', dataIndex: 'side', key: 'side',
      render: (v: string) => (
        <Tag color={v === 'buy' ? 'green' : 'red'}>{v === 'buy' ? '买入' : '卖出'}</Tag>
      ),
    },
    { title: '价格', dataIndex: 'price', key: 'price', render: (v: number) => v?.toFixed(3) },
    { title: '数量', dataIndex: 'quantity', key: 'quantity' },
    { title: '费用', dataIndex: 'fee', key: 'fee', render: (v: number) => v?.toFixed(2) },
    {
      title: '单笔盈亏', dataIndex: 'pnl', key: 'pnl',
      render: (v?: number) =>
        v == null ? '—' : (
          <span style={{ color: v >= 0 ? '#10b981' : '#ef4444' }}>
            {v >= 0 ? '+' : ''}
            {v.toFixed(2)}
          </span>
        ),
    },
    { title: '原因', dataIndex: 'reason', key: 'reason' },
  ]

  return (
    <div>
      <Typography.Title level={4}>策略回测</Typography.Title>
      {/* 页面强制风险提示（开发文档 §4 模块5）——始终可见 */}
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message={`⚠️ ${RISK_NOTICE}`}
        description="历史回测结果由撮合引擎模拟生成（T+1/涨跌停限制/停牌顺延/滑点/手续费），仅用于策略研究，不构成投资建议。"
      />
      <Row gutter={16}>
        <Col xs={24} lg={8}>
          <Card title="回测参数">
            <Form
              form={form}
              layout="vertical"
              onFinish={onFinish}
              initialValues={{
                range: [dayjs().subtract(1, 'year'), dayjs()],
                initial_capital: 100000,
                fee_rate: 0.0005,
                slippage: 0.001,
                limit_pct: 0.1,
                max_positions: 5,
              }}
            >
              <Form.Item name="strategy_id" label="选股策略" rules={[{ required: true, message: '请选择策略' }]}>
                <Select
                  placeholder="选择已保存策略（选股器页保存）"
                  options={strategies.map((s) => ({ value: s.id, label: s.name }))}
                />
              </Form.Item>
              <Form.Item name="range" label="时间区间" rules={[{ required: true }]}>
                <DatePicker.RangePicker style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="initial_capital" label="初始资金" rules={[{ required: true }]}>
                <InputNumber min={1000} step={10000} style={{ width: '100%' }} />
              </Form.Item>
              <Space size={12}>
                <Form.Item name="fee_rate" label="手续费率">
                  <InputNumber min={0} max={0.01} step={0.0001} style={{ width: 140 }} />
                </Form.Item>
                <Form.Item name="slippage" label="单边滑点">
                  <InputNumber min={0} max={0.05} step={0.001} style={{ width: 140 }} />
                </Form.Item>
              </Space>
              <Space size={12}>
                <Form.Item name="limit_pct" label="涨跌停幅度">
                  <InputNumber min={0.05} max={0.3} step={0.05} style={{ width: 140 }} />
                </Form.Item>
                <Form.Item name="max_positions" label="最大持仓数">
                  <InputNumber min={1} max={20} style={{ width: 140 }} />
                </Form.Item>
              </Space>
              <Button type="primary" htmlType="submit" block loading={running}>
                运行回测
              </Button>
            </Form>
          </Card>
        </Col>
        <Col xs={24} lg={16}>
          <Card title="回测结果">
            {running && (
              <div style={{ textAlign: 'center', padding: 48 }}>
                <Spin tip="回测运行中（异步任务，长周期请耐心等待）…" />
              </div>
            )}
            {!running && record && record.status === 'success' && (
              <>
                <Row gutter={12} style={{ marginBottom: 16 }}>
                  <Col span={6}>
                    <Statistic
                      title="总收益"
                      value={pct(result.cumulative_return)}
                      valueStyle={{ color: (result.cumulative_return ?? 0) >= 0 ? '#10b981' : '#ef4444' }}
                    />
                  </Col>
                  <Col span={6}>
                    <Statistic title="胜率" value={pct(result.win_rate)} />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title="最大回撤"
                      value={result.max_drawdown == null ? '—' : `-${pct(result.max_drawdown)}`}
                      valueStyle={{ color: '#ef4444' }}
                    />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title="盈亏比"
                      value={result.profit_loss_ratio ?? '—'}
                      precision={2}
                    />
                  </Col>
                </Row>
                <EChart
                  height={260}
                  option={{
                    backgroundColor: 'transparent',
                    grid: { left: 56, right: 16, top: 16, bottom: 28 },
                    tooltip: { trigger: 'axis' },
                    xAxis: {
                      type: 'category',
                      data: curve.map((p) => p.date),
                      axisLabel: { color: '#94a3b8' },
                    },
                    yAxis: {
                      type: 'value',
                      scale: true,
                      axisLabel: { color: '#94a3b8' },
                    },
                    series: [
                      {
                        name: '账户净值',
                        type: 'line',
                        showSymbol: false,
                        smooth: true,
                        data: curve.map((p) => p.equity),
                        areaStyle: { opacity: 0.15 },
                        lineStyle: { color: '#2563eb' },
                      },
                    ],
                  }}
                />
                <Descriptions size="small" column={4} style={{ marginTop: 8 }}>
                  <Descriptions.Item label="期末资产">
                    {result.final_equity?.toLocaleString?.() ?? '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label="平仓笔数">{result.closed_trades ?? '—'}</Descriptions.Item>
                  <Descriptions.Item label="成交笔数">{trades.length}</Descriptions.Item>
                  <Descriptions.Item label="引擎">{result.engine ?? '—'}</Descriptions.Item>
                </Descriptions>
                <Typography.Title level={5} style={{ marginTop: 16 }}>
                  交易明细
                </Typography.Title>
                <Table
                  rowKey="key"
                  columns={columns}
                  dataSource={trades}
                  size="small"
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 720 }}
                />
              </>
            )}
            {!running && record && record.status === 'failed' && (
              <Alert type="error" showIcon message={`回测失败：${result.error || '未知错误'}`} />
            )}
            {!running && !record && (
              <Typography.Text type="secondary">
                配置参数后点击「运行回测」；结果将展示净值曲线、统计指标与逐笔交易明细。
              </Typography.Text>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
