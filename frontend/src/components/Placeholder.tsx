import { Card, Typography } from 'antd'

/** 页面占位：T4.x 实现真实功能 */
export default function Placeholder({ title, task }: { title: string; task: string }) {
  return (
    <Card>
      <Typography.Title level={3}>{title}</Typography.Title>
      <Typography.Paragraph type="secondary">本页将在 {task} 实现（见 docs/开发进度文档.md）。</Typography.Paragraph>
    </Card>
  )
}
