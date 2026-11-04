import { Button, Card, Col, Empty, Row, Select, Spin, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { request } from '../api/client'
import type { EChartsOption } from 'echarts'
import EChart from '../components/EChart'

/** 大盘仪表盘（T4.3，开发文档 §3.2）：四卡片 + 情绪趋势 + 主线板块资金 */

interface TrendPoint {
  date: string
  sentiment_score: number | null
}

interface SectorFund {
  name: string
  moneyflow: number | null
  heat: string | null
}

interface DashboardMarket {
  trade_date: string | null
  sentiment_score: number | null
  limit_up_count: number | null
  limit_down_count: number | null
  max_consecutive: number | null
  consecutive_ladder: Record<string, number>
  main_moneyflow: number | null // 东财口径（北向净流入已季度披露不可实时取，§10.7）
  up_count: number | null
  down_count: number | null
  sentiment_trend: TrendPoint[]
  main_sectors: SectorFund[]
}

const fmt = (v: number | null | undefined, suffix = ''): string =>
  v === null || v === undefined ? '—' : `${v}${suffix}`

const fmtYi = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}亿`

function sentimentTone(score: number | null | undefined): string {
  if (score === null || score === undefined) return '—'
  if (score >= 70) return '偏强'
  if (score >= 50) return '中性偏强'
  if (score >= 30) return '中性偏弱'
  return '偏弱'
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardMarket | null>(null)
  const [days, setDays] = useState(20)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const d = await request<DashboardMarket>(`/api/dashboard/market?days=${days}`)
      setData(d)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    void load()
  }, [load])

  const trendOption = useMemo<EChartsOption>(
    () => ({
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 20, top: 20, bottom: 30 },
      xAxis: {
        type: 'category' as const,
        data: (data?.sentiment_trend ?? []).map((p) => p.date),
        axisLabel: { color: '#94a3b8' },
      },
      yAxis: {
        type: 'value' as const,
        min: 0,
        max: 100,
        axisLabel: { color: '#94a3b8' },
      },
      series: [
        {
          name: '情绪分',
          type: 'line' as const,
          smooth: true,
          areaStyle: { opacity: 0.15 },
          itemStyle: { color: '#2563eb' },
          data: (data?.sentiment_trend ?? []).map((p) => p.sentiment_score),
        },
      ],
    }),
    [data?.sentiment_trend],
  )

  const sectorOption = useMemo<EChartsOption>(() => {
    const sectors = data?.main_sectors ?? []
    return {
      tooltip: { trigger: 'axis' as const },
      grid: { left: 70, right: 30, top: 10, bottom: 30 },
      xAxis: {
        type: 'value' as const,
        axisLabel: { color: '#94a3b8', formatter: '{value}亿' },
      },
      yAxis: {
        type: 'category' as const,
        data: sectors.map((s) => s.name),
        axisLabel: { color: '#94a3b8' },
      },
      series: [
        {
          name: '主力净流入',
          type: 'bar' as const,
          data: sectors.map((s) => ({
            value: s.moneyflow ?? 0,
            itemStyle: { color: (s.moneyflow ?? 0) >= 0 ? '#10b981' : '#ef4444' },
          })),
        },
      ],
    }
  }, [data?.main_sectors])

  if (loading && !data) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin />
      </div>
    )
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>大盘仪表盘</h1>
        <Typography.Text type="secondary">
          全市场情绪、资金、主线板块概览{data?.trade_date ? ` · 交易日 ${data.trade_date}` : ''}
        </Typography.Text>
      </div>

      {error && (
        <Card style={{ marginBottom: 16, borderColor: '#ef4444' }}>
          <Typography.Text type="danger">{error}</Typography.Text>
          <Button size="small" style={{ marginLeft: 12 }} onClick={() => void load()}>
            重试
          </Button>
        </Card>
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Typography.Text type="secondary">市场情绪得分</Typography.Text>
            <div style={{ fontSize: 32, fontWeight: 700, color: '#f59e0b' }}>
              {fmt(data?.sentiment_score)}
            </div>
            <Typography.Text type="secondary">{sentimentTone(data?.sentiment_score)}</Typography.Text>
          </Card>
        </Col>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Typography.Text type="secondary">涨停家数</Typography.Text>
            <div style={{ fontSize: 32, fontWeight: 700, color: '#10b981' }}>
              {fmt(data?.limit_up_count)}
            </div>
            <Typography.Text type="secondary">
              连板：{fmt(data?.max_consecutive)}家
            </Typography.Text>
          </Card>
        </Col>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Typography.Text type="secondary">主力资金</Typography.Text>
            <div
              style={{
                fontSize: 32,
                fontWeight: 700,
                color: (data?.main_moneyflow ?? 0) >= 0 ? '#10b981' : '#ef4444',
              }}
            >
              {fmtYi(data?.main_moneyflow)}
            </div>
            <Typography.Text type="secondary">当日净流入（东财口径）</Typography.Text>
          </Card>
        </Col>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Typography.Text type="secondary">涨跌家数</Typography.Text>
            <div style={{ fontSize: 32, fontWeight: 700 }}>
              <span style={{ color: '#10b981' }}>{fmt(data?.up_count)}</span>
              {' / '}
              <span style={{ color: '#ef4444' }}>{fmt(data?.down_count)}</span>
            </div>
            <Typography.Text type="secondary">上涨 / 下跌</Typography.Text>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card
            title="情绪趋势曲线"
            extra={
              <Select
                value={days}
                onChange={setDays}
                options={[
                  { value: 20, label: '近20个交易日' },
                  { value: 60, label: '近60个交易日' },
                ]}
                size="small"
              />
            }
          >
            {data?.sentiment_trend?.length ? (
              <EChart option={trendOption} height={260} />
            ) : (
              <Empty description="暂无情绪数据（待采集入库）" style={{ padding: '60px 0' }} />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="主线板块资金">
            {data?.main_sectors?.length ? (
              <EChart option={sectorOption} height={260} />
            ) : (
              <Empty description="暂无板块资金数据" style={{ padding: '60px 0' }} />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
