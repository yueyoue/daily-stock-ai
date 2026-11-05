import {
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import type { Dayjs } from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { hotApi, type HotTopicItem } from '../api/client'

/** 热点题材库（T6.6，对齐原型 docs/AI投研工作台.html「热点题材库」页）：
 *  热度徽章卡片（高/中/低）+ 产业链标签 + 关联个股；支持板块/关键词/日期检索（T6.3 归档数据）。 */

const HEAT_TONE: Record<string, { color: string; label: string }> = {
  高: { color: 'warning', label: '高热度' },
  中: { color: 'blue', label: '中热度' },
  低: { color: 'default', label: '低热度' },
}

export default function HotPage() {
  const [items, setItems] = useState<HotTopicItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [keyword, setKeyword] = useState('')
  const [heat, setHeat] = useState<string | undefined>(undefined)
  const [day, setDay] = useState<Dayjs | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const p = await hotApi.list({
        keyword: keyword.trim() || undefined,
        heat,
        date: day ? day.format('YYYY-MM-DD') : undefined,
      })
      setItems(p.items)
      setTotal(p.total)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [keyword, heat, day])

  useEffect(() => {
    load()
  }, [load])

  const renderCard = (item: HotTopicItem) => {
    const tone = HEAT_TONE[item.heat] ?? HEAT_TONE['低']
    const chain = (item.industry_chain || '')
      .split(/[、,，;；]/)
      .map((s) => s.trim())
      .filter(Boolean)
    return (
      <Col xs={24} md={12} xl={8} key={item.id} style={{ marginBottom: 16 }}>
        <Card size="small" style={{ height: '100%' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <Typography.Text strong style={{ fontSize: 15 }}>
              {item.title}
            </Typography.Text>
            <Tag color={tone.color}>{tone.label}</Tag>
          </div>
          <div style={{ marginTop: 6, fontSize: 12, color: '#94a3b8' }}>
            产业链：
            {chain.length ? chain.map((c) => <Tag key={c}>{c}</Tag>) : '—'}
          </div>
          <div style={{ marginTop: 8, fontSize: 12 }}>
            关联个股：
            {item.related_codes.length ? (
              item.related_codes.slice(0, 8).map((c) => (
                <Tag key={c} color="blue">
                  {c}
                </Tag>
              ))
            ) : (
              <Typography.Text type="secondary">—</Typography.Text>
            )}
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: '#64748b' }}>归档日期：{item.date}</div>
        </Card>
      </Col>
    )
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>热点题材库</h1>
        <Typography.Text type="secondary">
          自动归档每日热点，产业链标签 + 关联个股（共 {total} 条）
        </Typography.Text>
      </div>

      <Card style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input
            placeholder="板块 / 产业链关键词"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onPressEnter={load}
            allowClear
            style={{ width: 200 }}
          />
          <Select
            allowClear
            placeholder="热度"
            style={{ width: 120 }}
            value={heat}
            onChange={setHeat}
            options={[
              { value: '高', label: '高热度' },
              { value: '中', label: '中热度' },
              { value: '低', label: '低热度' },
            ]}
          />
          <DatePicker value={day} onChange={setDay} placeholder="归档日期" style={{ width: 150 }} />
          <Button type="primary" onClick={load}>
            检索
          </Button>
        </Space>
      </Card>

      {loading ? (
        <Card loading />
      ) : items.length === 0 ? (
        <Empty description="暂无热点归档（可在任务监控触发采集，或先跑热点归档任务）" />
      ) : (
        <Row gutter={16}>{items.map(renderCard)}</Row>
      )}
    </div>
  )
}