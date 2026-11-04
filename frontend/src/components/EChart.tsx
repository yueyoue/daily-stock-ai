import * as echarts from 'echarts'
import { useEffect, useRef } from 'react'

/** ECharts React 薄封装（T4.3）：初始化/自适应/销毁 + option 更新 */
export default function EChart({
  option,
  height = 240,
}: {
  option: echarts.EChartsOption
  height?: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!ref.current) return
    chartRef.current = echarts.init(ref.current)
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option)
  }, [option])

  return <div ref={ref} style={{ height, width: '100%' }} />
}
