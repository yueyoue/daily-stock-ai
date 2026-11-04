import {
  Button,
  Card,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { datasourceApi } from '../api/client'
import type { SourceInterface, SourcePayload } from '../api/client'

/** 数据源接口配置（T4.8，开发文档 §2.4：增删改/启停/优先级/连通测试/健康状态） */

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export default function DataSourcePanel() {
  const [items, setItems] = useState<SourceInterface[]>([])
  const [loading, setLoading] = useState(true)
  const [filterType, setFilterType] = useState<string | undefined>()
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<SourceInterface | null>(null)
  const [saving, setSaving] = useState(false)
  const [testingId, setTestingId] = useState<number | null>(null)

  // 编辑表单（受控字段）
  const [fDataType, setFDataType] = useState('')
  const [fName, setFName] = useState('')
  const [fImplKind, setFImplKind] = useState('py_func')
  const [fImplRef, setFImplRef] = useState('')
  const [fParamsText, setFParamsText] = useState('{}')
  const [fRole, setFRole] = useState('备用')
  const [fPriority, setFPriority] = useState(10)
  const [fQps, setFQps] = useState(1)
  const [fTimeout, setFTimeout] = useState(30)
  const [fEnabled, setFEnabled] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const p = await datasourceApi.list(filterType)
      setItems(p.items)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '接口配置加载失败')
    } finally {
      setLoading(false)
    }
  }, [filterType])

  useEffect(() => {
    void load()
  }, [load])

  const typeOptions = useMemo(() => {
    const seen = new Set(items.map((i) => i.data_type))
    return [...seen].map((t) => ({ value: t, label: t }))
  }, [items])

  const openCreate = () => {
    setEditing(null)
    setFDataType('')
    setFName('')
    setFImplKind('py_func')
    setFImplRef('')
    setFParamsText('{}')
    setFRole('备用')
    setFPriority(10)
    setFQps(1)
    setFTimeout(30)
    setFEnabled(true)
    setModalOpen(true)
  }

  const openEdit = (r: SourceInterface) => {
    setEditing(r)
    setFDataType(r.data_type)
    setFName(r.name)
    setFImplKind(r.impl_kind)
    setFImplRef(r.impl_ref)
    setFParamsText(JSON.stringify(r.params_template ?? {}, null, 2))
    setFRole(r.role)
    setFPriority(r.priority)
    setFQps(r.qps_limit)
    setFTimeout(r.timeout_sec)
    setFEnabled(r.enabled)
    setModalOpen(true)
  }

  const submit = async () => {
    if (!fDataType.trim() || !fName.trim() || !fImplRef.trim()) {
      message.warning('数据类型 / 接口名称 / 接口实现为必填')
      return
    }
    let params: Record<string, unknown>
    try {
      params = fParamsText.trim() ? (JSON.parse(fParamsText) as Record<string, unknown>) : {}
    } catch {
      message.error('参数模板不是合法 JSON')
      return
    }
    const payload: SourcePayload = {
      data_type: fDataType.trim(),
      name: fName.trim(),
      impl_kind: fImplKind,
      impl_ref: fImplRef.trim(),
      params_template: params,
      role: fRole,
      priority: fPriority,
      qps_limit: fQps,
      timeout_sec: fTimeout,
      enabled: fEnabled,
    }
    setSaving(true)
    try {
      if (editing) {
        await datasourceApi.update(editing.id, payload)
        message.success('接口已更新，选路立即生效')
      } else {
        await datasourceApi.create(payload)
        message.success('接口已登记')
      }
      setModalOpen(false)
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const patch = async (r: SourceInterface, data: Partial<SourcePayload>, tip: string) => {
    try {
      await datasourceApi.update(r.id, data)
      message.success(tip)
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '操作失败')
    }
  }

  const runTest = async (r: SourceInterface) => {
    setTestingId(r.id)
    try {
      const res = await datasourceApi.test(r.id)
      if (res.ok) {
        message.success(`连通正常：试采 ${res.count} 条，耗时 ${res.duration_ms}ms`)
      } else {
        message.error(`连通失败：${res.error ?? '未知错误'}`)
      }
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '测试失败')
    } finally {
      setTestingId(null)
    }
  }

  const remove = async (r: SourceInterface) => {
    try {
      await datasourceApi.remove(r.id)
      message.success('接口已删除')
      await load()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  const healthView = (r: SourceInterface) => {
    if (!r.enabled) return <Tag>停用</Tag>
    if (r.last_error) {
      return (
        <Tooltip title={r.last_error}>
          <Tag color="error">最近错误</Tag>
        </Tooltip>
      )
    }
    if (r.last_ok_at) {
      return (
        <Tooltip title={`最近成功：${fmtTime(r.last_ok_at)}`}>
          <Tag color="success">✓ 正常</Tag>
        </Tooltip>
      )
    }
    return <Tag>未测试</Tag>
  }

  const columns: ColumnsType<SourceInterface> = [
    { title: '数据类型', dataIndex: 'data_type', key: 'data_type', width: 110 },
    {
      title: '接口名称 / 实现',
      key: 'impl',
      ellipsis: true,
      render: (_, r) => (
        <span>
          {r.name}
          <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }} ellipsis>
            {r.impl_kind} · {r.impl_ref}
          </Typography.Text>
        </span>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 80,
      render: (v: string) => <Tag color={v === '主源' ? 'blue' : 'gold'}>{v}</Tag>,
    },
    {
      title: '优先级',
      dataIndex: 'priority',
      key: 'priority',
      width: 110,
      render: (v: number, r) => (
        <Space size={2}>
          <span>{v}</span>
          <Button
            size="small"
            type="text"
            disabled={v <= 0}
            onClick={() => void patch(r, { priority: Math.max(0, v - 1) }, '优先级已上调')}
          >
            ↑
          </Button>
          <Button
            size="small"
            type="text"
            disabled={v >= 99}
            onClick={() => void patch(r, { priority: Math.min(99, v + 1) }, '优先级已下调')}
          >
            ↓
          </Button>
        </Space>
      ),
    },
    {
      title: 'QPS上限',
      dataIndex: 'qps_limit',
      key: 'qps_limit',
      width: 90,
      render: (v: number) => v,
    },
    {
      title: '超时',
      dataIndex: 'timeout_sec',
      key: 'timeout_sec',
      width: 80,
      render: (v: number) => `${v}s`,
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 70,
      render: (v: boolean, r) => (
        <Switch
          size="small"
          checked={v}
          onChange={(checked) =>
            void patch(r, { enabled: checked }, checked ? '已启用，选路立即生效' : '已停用')
          }
        />
      ),
    },
    {
      title: '健康状态',
      key: 'health',
      width: 110,
      render: (_, r) => healthView(r),
    },
    {
      title: '操作',
      key: 'actions',
      width: 170,
      render: (_, r) => (
        <Space size={4}>
          <Button size="small" loading={testingId === r.id} onClick={() => void runTest(r)}>
            测试
          </Button>
          <Button size="small" onClick={() => openEdit(r)}>
            编辑
          </Button>
          <Popconfirm title="确认删除该接口配置？" onConfirm={() => void remove(r)}>
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="数据源接口配置（可增删改 · 接口失效可随时更换，无需改代码）"
      extra={
        <Space wrap>
          <Select
            allowClear
            placeholder="按数据类型过滤"
            style={{ width: 160 }}
            value={filterType}
            options={typeOptions}
            onChange={(v) => setFilterType(v)}
          />
          <Button type="primary" onClick={openCreate}>
            新增接口
          </Button>
        </Space>
      }
    >
      <Table<SourceInterface>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        pagination={false}
      />
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
        💡 采集器按“数据类型 → 启用中优先级最高”自动选路，主源失败自动降级到备源，切换过程记录到
        data_source_log / data_reconcile。连通性测试为单接口试采 1 条。
      </Typography.Paragraph>

      <Modal
        title={editing ? '编辑接口' : '新增接口'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => void submit()}
        confirmLoading={saving}
        destroyOnClose
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>数据类型 *</div>
            <Input value={fDataType} onChange={(e) => setFDataType(e.target.value)} placeholder="如 kline / stock_info / finance" />
          </div>
          <div>
            <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>接口名称 *</div>
            <Input value={fName} onChange={(e) => setFName(e.target.value)} placeholder="展示名，如 通达信 mootdx 日线" />
          </div>
          <div>
            <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>接口实现（模块:类/函数 或 URL）*</div>
            <Input value={fImplRef} onChange={(e) => setFImplRef(e.target.value)} placeholder="app.collectors.kline:MootdxKlineCollector" />
          </div>
          <div>
            <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>参数模板（JSON）</div>
            <Input.TextArea rows={3} value={fParamsText} onChange={(e) => setFParamsText(e.target.value)} />
          </div>
          <Space wrap>
            <div>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>实现方式</div>
              <Select
                style={{ width: 120 }}
                value={fImplKind}
                options={[
                  { value: 'py_func', label: 'py_func' },
                  { value: 'http', label: 'http' },
                ]}
                onChange={(v) => setFImplKind(v)}
              />
            </div>
            <div>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>角色</div>
              <Select
                style={{ width: 100 }}
                value={fRole}
                options={[
                  { value: '主源', label: '主源' },
                  { value: '备用', label: '备用' },
                ]}
                onChange={(v) => setFRole(v)}
              />
            </div>
            <div>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>优先级（小者优先）</div>
              <InputNumber min={0} max={99} value={fPriority} onChange={(v) => setFPriority(v ?? 10)} />
            </div>
            <div>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>QPS 上限</div>
              <InputNumber min={0.1} max={10} step={0.1} value={fQps} onChange={(v) => setFQps(v ?? 1)} />
            </div>
            <div>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>超时（秒）</div>
              <InputNumber min={1} max={300} value={fTimeout} onChange={(v) => setFTimeout(v ?? 30)} />
            </div>
            <div>
              <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>启用</div>
              <Switch checked={fEnabled} onChange={setFEnabled} />
            </div>
          </Space>
        </Space>
      </Modal>
    </Card>
  )
}
