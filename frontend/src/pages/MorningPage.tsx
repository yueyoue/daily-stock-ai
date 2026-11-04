import ReportViewer from '../components/ReportViewer'

/** 盘前早报（T4.4，开发文档 §3.2：7 维度，交易日 08:00 自动生成） */
export default function MorningPage() {
  return (
    <ReportViewer
      type="morning"
      title="盘前早报"
      subtitle="交易日早8点自动生成（7维度），支持手动重生成与导出MD"
    />
  )
}
