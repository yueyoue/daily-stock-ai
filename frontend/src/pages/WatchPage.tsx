import {
  Button,
  Card,
  Col,
  Empty,
  Input,
  Popconfirm,
  Row,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { watchlistApi, type WatchItem } from '../api/client'

/** 掘金观察池（T6.5，开发文档 §4模块7）：快照预览（行情/ROE/市值）+ 概念维度分组 + 增删 */

export default function WatchPage() {
  const [items, setItems] = useState<WatchItem[]>([])
  const [loading, setLoading] = useState(true)
  const [dimension, setDimension] = useState<string | undefined>(undefined)
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [newDim, setNewDim] = useState('')
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const p = await watchlistApi.list(dimension)
      setItems(p.items)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [dimension])

  useEffect(() => {
    load()
  }, [load])

  const dimensions = useMemo(() => {
    const set = new Set<string>()
    items.forEach((i) => i.dimension && set.add(i.dimension))
    return [...set]
  }, [items])

  const grouped = useMemo(() => {
    const map = new Map<string, WatchItem[]>()
    items.forEach((i) => {
      const key = i.dimension || '未分组'
      map.set(key, [...(map.get(key) ?? []), i])
    })
    return [...map.entries()]
  }, [items])

  const addItem = async () => {
    if (!code.trim()) {
      message.warning('请输入代码')
      return
    }
    setAdding(true)
    try {
      await watchlistApi.add(code.trim(), name.trim(), newDim.trim())
      message.success('已加入观察池')
      setCode('')
      setName('')
      setNewDim('')
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '添加失败')
    } finally {
      setAdding(false)
    }
  }

  const removeItem = async (id: number) => {
    try {
      await watchlistApi.remove(id)
      message.success('已移出观察池')
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '移除失败')
    }
  }

  const renderCard = (item: WatchItem) => {
    const pct = item.pct_change
    return (
      <Col span={6} key={item.id} style={{ marginBottom: 16 }}>
        <Card size="small">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <Typography.Text strong>{item.code}</Typography.Text>
              <div>
                <Typography.Text type="secondary">{item.name || '—'}</Typography.Text>
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div
                style={{
                  fontSize: 18,
                  fontWeight: 700,
                  color: pct == null ? '#94a3b8' : pct >= 0 ? '#10b981' : '#ef4444',
                }}
              >
                {pct == null ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`}
              </div>
              <Typography.Text type="secondary">
                {item.close != null ? `收盘 ${item.close}` : ''}
              </Typography.Text>
            </div>
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: '#94a3b8' }}>
            <div>
              ROE：{item.roe ?? '—'}　市值：{item.market_cap != null ? `${item.market_cap}亿` : '—'}
            </div>
            <div>
              {item.industry && <Tag>{item.industry}</Tag>}
              {(item.concepts ?? []).slice(0, 3).map((c) => (
                <Tag key={c} color="blue">
                  {c}
                </Tag>
              ))}
            </div>
          </div>
          <div style={{ marginTop: 8, textAlign: 'right' }}>
            <Popconfirm title="确认移出观察池？" onConfirm={() => removeItem(item.id)}>
              <Button size="small" danger type="text">
                移除
              </Button>
            </Popconfirm>
          </div>
        </Card>
      </Col>
    )
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>掘金观察池</h1>
        <Typography.Text type="secondary">
          自选标的快照（行情/ROE/市值），按关注维度分组；选股/复盘可一键加入
        </Typography.Text>
      </div>

      <Card style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input
            placeholder="代码（如 000001）"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            style={{ width: 140 }}
          />
          <Input
            placeholder="名称（可选）"
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ width: 140 }}
          />
          <Input
            placeholder="关注维度（可选）"
            value={newDim}
            onChange={(e) => setNewDim(e.target.value)}
            style={{ width: 140 }}
          />
          <Button type="primary" loading={adding} onClick={addItem}>
            加入观察池
          </Button>
          <Select
            allowClear
            placeholder="维度筛选"
            style={{ width: 140 }}
            value={dimension}
            onChange={setDimension}
            options={dimensions.map((d) => ({ value: d, label: d }))}
          />
          <Button onClick={load}>刷新</Button>
        </Space>
      </Card>

      {loading ? (
        <Card loading />
      ) : items.length === 0 ? (
        <Empty description="观察池为空，添加标的或从选股/复盘一键加入" />
      ) : (
        grouped.map(([dim, list]) => (
          <div key={dim} style={{ marginBottom: 8 }}>
            <Typography.Title level={5} style={{ marginTop: 8 }}>
              {dim}
            </Typography.Title>
            <Row gutter={16}>{list.map(renderCard)}</Row>
          </div>
        ))
      )}
    </div>
  )
}
