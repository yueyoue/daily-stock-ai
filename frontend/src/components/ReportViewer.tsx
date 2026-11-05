import { Button, Card, Col, Empty, List, Row, Space, Spin, Typography, message } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { request } from '../api/client'

/** 报告工作区（T4.4/T4.5 共用）：历史列表 + Markdown 渲染 + 重新生成 + 导出 MD */

interface ReportItem {
  id: number
  type: string
  report_date: string
  title: string
  generated_at: string | null
  version: number
}

interface ReportDetail extends ReportItem {
  md_content: string
}

export default function ReportViewer({
  type,
  title,
  subtitle,
}: {
  type: 'morning' | 'afternoon'
  title: string
  subtitle: string
}) {
  const [items, setItems] = useState<ReportItem[]>([])
  const [selected, setSelected] = useState<ReportDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [regenerating, setRegenerating] = useState(false)

  const openReport = useCallback(async (id: number) => {
    try {
      setSelected(await request<ReportDetail>(`/api/report/${id}`))
    } catch (e) {
      message.error(e instanceof Error ? e.message : '报告加载失败')
    }
  }, [])

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const page = await request<{ items: ReportItem[]; total: number }>(
        `/api/report/list?type=${type}&page=1&page_size=20`,
      )
      setItems(page.items)
      if (page.items.length > 0) {
        await openReport(page.items[0].id)
      } else {
        setSelected(null)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [type, openReport])

  useEffect(() => {
    loadList()
  }, [loadList])

const regenerate = async () => {
    const reportDate = selected?.report_date ?? new Date().toISOString().slice(0, 10)
    setRegenerating(true)
    try {
      const r = await request<{
        id: number
        version: number
        ok: boolean
        attempts: number
        fallback: boolean
        llm_used: boolean
        problems: string[]
      }>('/api/report/regenerate', {
        method: 'POST',
        body: JSON.stringify({ type, report_date: reportDate }),
      })
      // T7.6：把生成质量如实反馈给用户（降级骨架 / 校验问题 / LLM 尝试次数）
      if (r.fallback) {
        message.warning('已生成结构化骨架（LLM 未就绪，配置 Key 后可重新生成）')
      } else if (r.problems && r.problems.length > 0) {
        message.warning(`已生成，但存在待人工复核项：${r.problems.join('；')}`)
      } else {
        message.success(`已重新生成（v${r.version}，尝试 ${r.attempts} 次）`)
      }
      await loadList()
      await openReport(r.id)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '重新生成失败')
    } finally {
      setRegenerating(false)
    }
  }

  const exportMd = () => {
    if (!selected) return
    const blob = new Blob([selected.md_content], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selected.report_date}-${title}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>{title}</h1>
        <Typography.Text type="secondary">{subtitle}</Typography.Text>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={8} lg={6}>
          <Card title="历史报告" styles={{ body: { padding: 12 } }}>
            {loading ? (
              <div style={{ textAlign: 'center', padding: 24 }}>
                <Spin size="small" />
              </div>
            ) : items.length === 0 ? (
              <Empty description="暂无报告" />
            ) : (
              <List
                size="small"
                dataSource={items}
                renderItem={(item) => {
                  const active = selected?.id === item.id
                  return (
                    <List.Item
                      onClick={() => openReport(item.id)}
                      style={{
                        cursor: 'pointer',
                        background: active ? 'rgba(37,99,235,0.15)' : undefined,
                        borderLeft: active ? '3px solid #2563eb' : '3px solid transparent',
                        padding: '8px 12px',
                      }}
                    >
                      {item.report_date} {title}
                      {item.version > 1 ? ` (v${item.version})` : ''}
                    </List.Item>
                  )
                }}
              />
            )}
<Button
              block
              style={{ marginTop: 12, borderColor: '#2563eb', color: '#2563eb' }}
              loading={regenerating}
              onClick={regenerate}
            >
              {items.length === 0 || !selected ? '生成当日报告' : '重新生成当日报告'}
            </Button>
          </Card>
        </Col>
        <Col xs={24} sm={16} lg={18}>
          <Card
            title={
              selected
                ? `${selected.report_date} ${title}${selected.version > 1 ? `（v${selected.version}）` : ''}`
                : title
            }
            extra={
              <Space>
                <Button size="small" disabled={!selected} onClick={exportMd}>
                  导出MD
                </Button>
              </Space>
            }
          >
{selected ? (
              <div className="md-body">
                {/* T7.6：展示生成时间与版本，便于核对定时任务产出 */}
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 12 }}>
                  版本 v{selected.version}
                  {selected.generated_at ? ` · 生成时间 ${selected.generated_at.replace('T', ' ').slice(0, 19)}` : ''}
                </Typography.Paragraph>
                <ReactMarkdown>{selected.md_content}</ReactMarkdown>
              </div>
            ) : (
              <Empty
                description="暂无报告内容，可点左侧「生成当日报告」"
                style={{ padding: '80px 0' }}
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
