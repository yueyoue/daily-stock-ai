import {
  Button,
  Card,
  DatePicker,
  Empty,
  Input,
  Pagination,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import type { Dayjs } from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { newsApi, type ClassifyStats, type NewsItem } from '../api/client'

/** 资讯中心（T6.6，对齐原型「资讯中心」页）：影响等级标签流 + 检索。
 *  降噪口径（T6.2）：默认隐藏「无关」；可切「显示全部」；未分级资讯仍展示。 */

const LEVEL_TONE: Record<string, { color: string; label: string }> = {
  高: { color: 'error', label: '高影响' },
  中: { color: 'blue', label: '中影响' },
  低: { color: 'default', label: '低影响' },
  无关: { color: 'default', label: '无关' },
  '': { color: 'warning', label: '未分级' },
}

const PAGE_SIZE = 20

export default function NewsPage() {
  const [items, setItems] = useState<NewsItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [classifying, setClassifying] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [level, setLevel] = useState<string | undefined>(undefined)
  const [source, setSource] = useState('')
  const [day, setDay] = useState<Dayjs | null>(null)
  const [allVisible, setAllVisible] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const p = await newsApi.list({
        keyword: keyword.trim() || undefined,
        impactLevel: level || undefined,
        source: source.trim() || undefined,
        from: day ? day.format('YYYY-MM-DD') : undefined,
        to: day ? day.format('YYYY-MM-DD') : undefined,
        onlyRelevant: allVisible ? false : undefined,
        page,
        pageSize: PAGE_SIZE,
      })
      setItems(p.items)
      setTotal(p.total)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [keyword, level, source, day, allVisible, page])

  useEffect(() => {
    load()
  }, [load])

  const runClassify = async () => {
    setClassifying(true)
    try {
      const s: ClassifyStats = await newsApi.classify(50, 20)
      if (s.pending === 0) {
        message.info('没有未分级的资讯需要处理')
      } else if (s.ok) {
        message.success(
          `降噪分级完成：高 ${s.classified['高']} / 中 ${s.classified['中']} / 低 ${s.classified['低']} / 无关 ${s.irrelevant}`,
        )
      } else {
        message.warning(
          `LLM 不可用，${s.unclassified} 条保持「未分级」原样（数据未丢失）：${s.last_error}`,
        )
      }
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '分级失败')
    } finally {
      setClassifying(false)
    }
  }

  const renderItem = (item: NewsItem) => {
    const tone = LEVEL_TONE[item.impact_level] ?? LEVEL_TONE['']
    const time = item.published_at ? item.published_at.replace('T', ' ').slice(0, 16) : '时间未知'
    return (
      <div key={item.id} style={{ display: 'flex', gap: 12 }}>
        <Tag color={tone.color} style={{ flex: '0 0 auto', alignSelf: 'flex-start' }}>
          {tone.label}
        </Tag>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 500, wordBreak: 'break-word' }}>{item.title}</div>
          {item.content && (
            <Tooltip title={item.content}>
              <div
                style={{
                  fontSize: 12,
                  color: '#94a3b8',
                  marginTop: 2,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {item.content}
              </div>
            </Tooltip>
          )}
          <div style={{ fontSize: 12, color: '#64748b', marginTop: 4 }}>
            {time}
            {item.source ? `　来源：${item.source}` : ''}
            {item.url ? (
              <Typography.Link href={item.url} target="_blank" style={{ marginLeft: 8 }}>
                原文
              </Typography.Link>
            ) : null}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>资讯中心</h1>
        <Typography.Text type="secondary">
          AI 降噪过滤，只保留影响盘面的资讯（默认隐藏「无关」；共 {total} 条）
        </Typography.Text>
      </div>

      <Card style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input
            placeholder="关键词（标题/内容）"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onPressEnter={() => setPage(1)}
            allowClear
            style={{ width: 180 }}
          />
          <Select
            allowClear
            placeholder="影响等级"
            style={{ width: 140 }}
            value={level}
            onChange={(v) => {
              setLevel(v)
              setPage(1)
            }}
            options={[
              { value: '高', label: '高影响' },
              { value: '中', label: '中影响' },
              { value: '低', label: '低影响' },
              { value: '无关', label: '无关' },
            ]}
          />
          <Input
            placeholder="来源"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            onPressEnter={() => setPage(1)}
            allowClear
            style={{ width: 120 }}
          />
          <DatePicker
            value={day}
            onChange={(d) => {
              setDay(d)
              setPage(1)
            }}
            placeholder="日期"
            style={{ width: 150 }}
          />
          <Space size={4}>
            <Typography.Text type="secondary">显示全部（含无关）</Typography.Text>
            <Switch
              checked={allVisible}
              onChange={(v) => {
                setAllVisible(v)
                setPage(1)
              }}
            />
          </Space>
          <Button type="primary" onClick={() => setPage(1)}>
            检索
          </Button>
          <Button loading={classifying} onClick={runClassify}>
            AI 降噪分级
          </Button>
        </Space>
      </Card>

      {loading ? (
        <Card loading />
      ) : items.length === 0 ? (
        <Empty description="暂无资讯（可在任务监控触发采集；或点「AI 降噪分级」处理未分级资讯）" />
      ) : (
        <>
          <Card>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              {items.map(renderItem)}
            </Space>
          </Card>
          <div style={{ marginTop: 16, textAlign: 'right' }}>
            <Pagination
              current={page}
              pageSize={PAGE_SIZE}
              total={total}
              showSizeChanger={false}
              onChange={setPage}
            />
          </div>
        </>
      )}
    </div>
  )
}