import { Card, Col, Empty, Row } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import { request } from '../api/client'
import type { EChartsOption } from 'echarts'
import EChart from '../components/EChart'
import ReportViewer from '../components/ReportViewer'

interface MarketBrief {
  limit_up_count: number | null
  max_consecutive: number | null
  limit_down_count: number | null
}

/** 盘后复盘（T4.5，开发文档 §3.2）：复盘报告 + 统计图表（涨停/连板/跌停） */
export default function AfternoonPage() {
  const [brief, setBrief] = useState<MarketBrief | null>(null)

  useEffect(() => {
    request<MarketBrief>('/api/dashboard/market?days=5')
      .then(setBrief)
      .catch(() => setBrief(null))
  }, [])

  const chartOption = useMemo<EChartsOption>(
    () => ({
      tooltip: { trigger: 'axis' as const },
      grid: { left: 50, right: 20, top: 20, bottom: 30 },
      xAxis: {
        type: 'category' as const,
        data: ['涨停', '连板', '跌停'],
        axisLabel: { color: '#94a3b8' },
      },
      yAxis: { type: 'value' as const, axisLabel: { color: '#94a3b8' } },
      series: [
        {
          name: '家数',
          type: 'bar' as const,
          barWidth: 48,
          data: [
            { value: brief?.limit_up_count ?? 0, itemStyle: { color: '#10b981' } },
            { value: brief?.max_consecutive ?? 0, itemStyle: { color: '#2563eb' } },
            { value: brief?.limit_down_count ?? 0, itemStyle: { color: '#ef4444' } },
          ],
        },
      ],
    }),
    [brief],
  )

  return (
    <div>
      <ReportViewer
        type="afternoon"
        title="盘后复盘"
        subtitle="交易日17点自动生成：情绪打分、涨停梯队、主线、策略验证、次日观察计划"
      />
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card title="复盘统计（涨停 / 连板 / 跌停）">
            {brief ? <EChart option={chartOption} height={220} /> : <Empty style={{ padding: 40 }} />}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
