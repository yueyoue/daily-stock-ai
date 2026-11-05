import { Button, Card, Divider, Input, Space, Switch, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { authApi, configApi } from '../api/client'

/** 模型 / 账号 / 定时任务（T4.9 + T7.5 + T7.6）：
 *  LLM 切换+Key 遮蔽+连通测试、修改密码、报告与采集定时/开关、失败告警 webhook */

function ToggleRow({
  label,
  checked,
  onChange,
  hint,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  hint?: string
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <Switch checked={checked} onChange={onChange} />
      <span style={{ fontSize: 13 }}>{label}</span>
      {hint ? <span style={{ fontSize: 12, opacity: 0.6 }}>{hint}</span> : null}
    </div>
  )
}

export default function ModelAccountPanel() {
  const [llmModel, setLlmModel] = useState('')
  const [llmKey, setLlmKey] = useState('') // 只进不出：留空=不修改
  const [keyMasked, setKeyMasked] = useState('')

  // 定时任务（T7.5）：报告/采集时间与开关
  const [amCron, setAmCron] = useState('')
  const [pmCron, setPmCron] = useState('')
  const [collectAmCron, setCollectAmCron] = useState('')
  const [collectPmCron, setCollectPmCron] = useState('')
  const [reportAmOn, setReportAmOn] = useState(true)
  const [reportPmOn, setReportPmOn] = useState(true)
  const [collectAmOn, setCollectAmOn] = useState(true)
  const [collectPmOn, setCollectPmOn] = useState(true)

  // 失败告警 webhook（T7.6）：空串 = 只在日志与任务日志留痕
  const [alertWebhook, setAlertWebhook] = useState('')

  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)

  const [oldPass, setOldPass] = useState('')
  const [newPass, setNewPass] = useState('')
  const [confirmPass, setConfirmPass] = useState('')
  const [changing, setChanging] = useState(false)

  const reload = async () => {
    try {
      const c = await configApi.get()
      setLlmModel(c.llm_model)
      setAmCron(c.report_am_cron)
      setPmCron(c.report_pm_cron)
      setCollectAmCron(c.collect_am_cron)
      setCollectPmCron(c.collect_pm_cron)
      setReportAmOn(c.report_am_enabled)
      setReportPmOn(c.report_pm_enabled)
      setCollectAmOn(c.collect_am_enabled)
      setCollectPmOn(c.collect_pm_enabled)
      setAlertWebhook(c.alert_webhook ?? '')
      setKeyMasked(c.llm_api_key_masked)
      setLlmKey('') // 保存后清空输入框，永不回显明文
    } catch (e) {
      message.error(e instanceof Error ? e.message : '配置加载失败')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      const payload: Parameters<typeof configApi.save>[0] = {
        llm_model: llmModel,
        report_am_cron: amCron,
        report_pm_cron: pmCron,
        collect_am_cron: collectAmCron,
        collect_pm_cron: collectPmCron,
        report_am_enabled: reportAmOn,
        report_pm_enabled: reportPmOn,
        collect_am_enabled: collectAmOn,
        collect_pm_enabled: collectPmOn,
        alert_webhook: alertWebhook.trim(),
      }
      if (llmKey.trim()) payload.llm_api_key = llmKey.trim() // 空=不修改
      await configApi.save(payload)
      message.success('已保存（开关与告警地址立即生效，cron 时间下次启动调度器时生效）')
      await reload()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const testLlm = async () => {
    setTesting(true)
    try {
      const r = await configApi.testLlm()
      if (r.ok) {
        message.success(`LLM 连通正常（${r.model}）`)
      } else {
        message.error(`LLM 连通失败：${r.error ?? '未知错误'}`)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '测试失败')
    } finally {
      setTesting(false)
    }
  }

  const changePassword = async () => {
    if (!oldPass || !newPass) {
      message.warning('请输入旧密码与新密码')
      return
    }
    if (newPass !== confirmPass) {
      message.warning('两次输入的新密码不一致')
      return
    }
    setChanging(true)
    try {
      await authApi.changePassword(oldPass, newPass)
      message.success('密码已修改，其他登录会话已失效')
      setOldPass('')
      setNewPass('')
      setConfirmPass('')
    } catch (e) {
      message.error(e instanceof Error ? e.message : '修改失败')
    } finally {
      setChanging(false)
    }
  }

  return (
    <Card title="模型 / 账号 / 定时任务">
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {/* LLM 模型与 Key */}
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>LLM 模型</div>
          <Input value={llmModel} onChange={(e) => setLlmModel(e.target.value)} placeholder="如 qwen-plus / kimi-k2 / mimo" />
        </div>
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>LLM API Key</div>
          <Input.Password
            value={llmKey}
            onChange={(e) => setLlmKey(e.target.value)}
            placeholder={keyMasked ? `已保存（${keyMasked}），留空不修改` : '未设置，请输入'}
          />
          <div style={{ fontSize: 12, opacity: 0.65, marginTop: 4 }}>
            Key 只保存到服务端配置，界面永远只显示尾号 4 位掩码
          </div>
        </div>
        <Button loading={testing} onClick={() => void testLlm()}>
          LLM 连通测试
        </Button>

        <Divider style={{ margin: '8px 0' }} />

        {/* 定时任务（T7.5，开发文档 §4.9） */}
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>盘前早报定时（cron）</div>
          <Input value={amCron} onChange={(e) => setAmCron(e.target.value)} placeholder="0 8 * * 1-5" />
        </div>
        <ToggleRow label="启用盘前早报" checked={reportAmOn} onChange={setReportAmOn} hint="交易日 08:00" />
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>盘后复盘定时（cron）</div>
          <Input value={pmCron} onChange={(e) => setPmCron(e.target.value)} placeholder="0 17 * * 1-5" />
        </div>
        <ToggleRow label="启用盘后复盘" checked={reportPmOn} onChange={setReportPmOn} hint="交易日 17:00" />
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>隔夜资讯采集定时（cron）</div>
          <Input value={collectAmCron} onChange={(e) => setCollectAmCron(e.target.value)} placeholder="30 7 * * *" />
        </div>
        <ToggleRow label="启用隔夜资讯采集" checked={collectAmOn} onChange={setCollectAmOn} hint="每日 07:30，非交易日仍抓" />
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>收盘数据采集定时（cron）</div>
          <Input value={collectPmCron} onChange={(e) => setCollectPmCron(e.target.value)} placeholder="30 15 * * 1-5" />
        </div>
        <ToggleRow label="启用收盘数据采集" checked={collectPmOn} onChange={setCollectPmOn} hint="交易日 15:30 → 16:00 指标缓存" />
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          cron 格式：分 时 日 月 周（默认 0 8 * * 1-5 = 交易日早 8 点）；交易日判断由调度器按交易日历前置过滤，
          非交易日不生成报告（只跑资讯线）。时区固定 Asia/Shanghai。
        </Typography.Paragraph>

        <Divider style={{ margin: '8px 0' }} />

        {/* 失败告警（T7.6，开发文档 §1.4） */}
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>失败告警 Webhook（可选）</div>
          <Input
            value={alertWebhook}
            onChange={(e) => setAlertWebhook(e.target.value)}
            placeholder="留空 = 只写日志与任务日志；填 URL 则失败时 POST JSON"
          />
          <div style={{ fontSize: 12, opacity: 0.65, marginTop: 4 }}>
            任务重试 3 次仍失败才触发：先写 ALERT 日志与任务日志（状态 warning），再向该地址 POST JSON
            （3 秒超时、best-effort，告警通道不通不影响任务本身）。
          </div>
        </div>

        <Button type="primary" loading={saving} onClick={() => void save()}>
          保存模型与定时配置
        </Button>

        <Divider style={{ margin: '8px 0' }} />

        {/* 修改密码 */}
        <div style={{ fontSize: 13, fontWeight: 500 }}>修改登录密码</div>
        <Input.Password value={oldPass} onChange={(e) => setOldPass(e.target.value)} placeholder="旧密码" />
        <Input.Password value={newPass} onChange={(e) => setNewPass(e.target.value)} placeholder="新密码" />
        <Input.Password value={confirmPass} onChange={(e) => setConfirmPass(e.target.value)} placeholder="确认新密码" />
        <Button loading={changing} onClick={() => void changePassword()}>
          修改密码
        </Button>
      </Space>
    </Card>
  )
}