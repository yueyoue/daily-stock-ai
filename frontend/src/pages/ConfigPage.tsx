import {
  Button,
  Card,
  Col,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Typography,
  message,
} from 'antd'
import { useEffect, useState } from 'react'
import { configApi } from '../api/client'
import DataSourcePanel from '../components/DataSourcePanel'
import ModelAccountPanel from '../components/ModelAccountPanel'

/** 系统配置（T4.7 存储与性能 / T4.8 数据源接口 / T4.9 模型·账号·定时） */

const ENGINE_OPTIONS = [
  { value: 'talib', label: 'TA-Lib（C语言，速度快）' },
  { value: 'pandas', label: 'pandas 内置实现（降级用）' },
]

export default function ConfigPage() {
  const [store, setStore] = useState(false)
  const [cacheDays, setCacheDays] = useState(3)
  const [engine, setEngine] = useState('talib')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    configApi
      .get()
      .then((c) => {
        setStore(c.tech_indicator_store)
        setCacheDays(c.indicator_cache_days)
        setEngine(c.indicator_engine)
      })
      .catch((e) => message.error(e instanceof Error ? e.message : '配置加载失败'))
      .finally(() => setLoading(false))
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      await configApi.save({
        tech_indicator_store: store,
        indicator_cache_days: cacheDays,
        indicator_engine: engine,
      })
      message.success('已保存，下次批处理任务按新模式执行')
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <Typography.Title level={2} style={{ marginTop: 0 }}>
        系统配置
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        数据源、LLM密钥、技术指标存储模式、定时任务开关
      </Typography.Paragraph>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card title="存储与性能" loading={loading}>
            <Space direction="vertical" size="large" style={{ width: '100%' }}>
              {/* 技术指标入库开关（模式 A/B） */}
              <div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div>
                    <div style={{ fontWeight: 500, fontSize: 14 }}>技术指标入库</div>
                    <div style={{ fontSize: 12, opacity: 0.65, marginTop: 2 }}>
                      开启→指标写入库表并每日增量（模式A）；关闭→走本地缓存方案（模式B，推荐）
                    </div>
                  </div>
                  <Switch checked={store} onChange={setStore} />
                </div>
                <div
                  style={{
                    marginTop: 8,
                    fontSize: 12,
                    padding: '6px 12px',
                    borderRadius: 8,
                    background: 'rgba(37, 99, 235, 0.12)',
                    color: '#60a5fa',
                  }}
                >
                  当前模式：{store ? '入库（stock_tech_indicator，模式A）' : '不入库（本地指标缓存，模式B）'}
                </div>
              </div>

              {/* 指标缓存有效天数 */}
              <div>
                <div style={{ opacity: 0.75, fontSize: 13, marginBottom: 4 }}>指标缓存有效天数</div>
                <InputNumber
                  min={1}
                  max={30}
                  style={{ width: '100%' }}
                  value={cacheDays}
                  onChange={(v) => setCacheDays(v ?? 3)}
                />
                <div style={{ fontSize: 12, opacity: 0.65, marginTop: 4 }}>
                  缓存文件保留天数，到期自动覆盖，不累积占用存储
                </div>
              </div>

              {/* 指标计算引擎 */}
              <div>
                <div style={{ opacity: 0.75, fontSize: 13, marginBottom: 4 }}>指标计算引擎</div>
                <Select
                  style={{ width: '100%' }}
                  value={engine}
                  options={ENGINE_OPTIONS}
                  onChange={(v) => setEngine(v)}
                />
              </div>

              <div style={{ fontSize: 12, opacity: 0.75, borderTop: '1px solid rgba(148,163,184,0.2)', paddingTop: 12 }}>
                💡 推荐：关闭入库，使用本地缓存方案。基础数据（K线/财务）约 6~7GB，指标不入库可省约
                10GB，选股响应毫秒级且不触发数据源风控。
              </div>

              <Button type="primary" loading={saving} onClick={() => void save()}>
                保存（立即生效）
              </Button>
            </Space>
          </Card>
        </Col>

        {/* 模型 / 账号 / 定时任务（T4.9） */}
        <Col xs={24} lg={10}>
          <ModelAccountPanel />
        </Col>

        {/* 数据源接口配置（T4.8） */}
        <Col xs={24}>
          <DataSourcePanel />
        </Col>
      </Row>
    </div>
  )
}
