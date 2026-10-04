import { useId } from 'react'
import { useViewport } from '@xyflow/react'

/** Canvas background: a 16px dot grid at rgba(255,255,255,0.04) that pans and zooms with the map. */
export function Pattern() {
  const { x, y, zoom } = useViewport()
  const id = useId()
  const t = 16 * zoom
  return (
    <svg className="react-flow__background" aria-hidden style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}>
      <pattern id={id} x={x % t} y={y % t} width={t} height={t} patternUnits="userSpaceOnUse">
        <circle cx={8 * zoom} cy={8 * zoom} r={Math.max(0.6, zoom)} fill="rgba(255,255,255,0.04)" />
      </pattern>
      <rect x="0" y="0" width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}
