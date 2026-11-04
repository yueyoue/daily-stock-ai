import { Button, Card, Divider, Input, Space, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { authApi, configApi } from '../api/client'

/** 模型 / 账号 / 定时任务（T4.9）：LLM 切换+Key 遮蔽+连通测试、修改密码、报告定时 */

export default function ModelAccountPanel() {
  const [llmModel, setLlmModel] = useState('')
  const [llmKey, setLlmKey] = useState('') // 只进不出：留空=不修改
  const [keyMasked, setKeyMasked] = useState('')
  const [amCron, setAmCron] = useState('')
  const [pmCron, setPmCron] = useState('')
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
      }
      if (llmKey.trim()) payload.llm_api_key = llmKey.trim() // 空=不修改
      await configApi.save(payload)
      message.success('已保存')
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

        {/* 定时任务 */}
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>盘前早报定时（cron）</div>
          <Input value={amCron} onChange={(e) => setAmCron(e.target.value)} placeholder="0 8 * * 1-5" />
        </div>
        <div>
          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 4 }}>盘后复盘定时（cron）</div>
          <Input value={pmCron} onChange={(e) => setPmCron(e.target.value)} placeholder="0 17 * * 1-5" />
        </div>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          cron 格式：分 时 星期…（默认 0 8 * * 1-5 = 交易日早 8 点）；交易日判断由调度器按交易日历前置过滤。
        </Typography.Paragraph>
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
