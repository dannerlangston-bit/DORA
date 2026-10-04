import { useViewport } from '@xyflow/react'
import type { LaneBand } from './blueprint'

/**
 * Blueprint lanes: alternating bands with a hairline between them, drawn under the cards and moving
 * with the map, as LD3 Map draws project lanes. Each lane's name is pinned to the left edge of the
 * screen (just right of the toolbar) so it stays readable however far the map is scrolled.
 */
export function Lanes({ lanes, width }: { lanes: LaneBand[]; width: number }) {
  const { x, y, zoom } = useViewport()
  return (
    <div className="react-flow__background pointer-events-none absolute inset-0 overflow-hidden" aria-hidden data-testid="lanes">
      <div className="absolute left-0 top-0" style={{ transform: `translate(${x}px, ${y}px) scale(${zoom})`, transformOrigin: '0 0' }}>
        {lanes.map((l, i) => (
          <div
            key={l.id}
            className="absolute border-t border-line-1"
            style={{ left: -2000, top: l.top, width: width + 4000, height: l.height, background: i % 2 ? 'rgba(255,255,255,0.015)' : 'transparent' }}
            data-lane={l.id}
          />
        ))}
      </div>
      {lanes.map((l) => {
        const top = y + l.top * zoom
        const bottom = top + l.height * zoom
        return (
          <div
            key={l.id}
            className="absolute left-[68px] flex items-baseline gap-2 rounded-2 border border-line-1 bg-raised px-2 py-1"
            style={{ top: Math.max(top + 8, Math.min(60, bottom - 36)) }}
            data-testid="lane-label"
          >
            <span className="text-xs font-medium uppercase tracking-[0.04em] text-ink-2">{l.label}</span>
            <span className="mono text-[11px] text-ink-3">{l.count}</span>
          </div>
        )
      })}
    </div>
  )
}
