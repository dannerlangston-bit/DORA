import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from '@xyflow/react'

export type LinkState = 'selected' | 'chain' | 'lit' | 'dim' | null
export type LinkEdgeData = { label: string; state: LinkState }
export type LinkEdge = Edge<LinkEdgeData, 'link'>

const STROKE: Record<string, string> = {
  selected: 'var(--orange)',
  chain: 'var(--orange-line)',
  lit: 'rgba(255,255,255,0.35)',
  base: 'rgba(255,255,255,0.16)',
}

/**
 * A link (DESIGN §8): LD3 Map's 1.5px curve plus a small arrowhead, because direction matters in an
 * explanation. Orange when selected or part of the chain being linked; hovering an event lights its
 * links and dims the rest. Labels show as an 11px mono chip at the middle.
 */
export function LinkEdgeView({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps<LinkEdge>) {
  const [path, lx, ly] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, curvature: 0.3 })
  const state = data?.state ?? null
  const stroke = STROKE[state === 'dim' || !state ? 'base' : state]
  const opacity = state === 'dim' ? 0.5 : 1
  const transition = 'stroke 180ms cubic-bezier(.2,.8,.2,1), fill 180ms cubic-bezier(.2,.8,.2,1), opacity 180ms cubic-bezier(.2,.8,.2,1)'
  // the curve always enters the card from the left, so the arrowhead always points right
  const head = `M ${targetX - 7} ${targetY - 4} L ${targetX} ${targetY} L ${targetX - 7} ${targetY + 4} Z`
  return (
    <>
      <BaseEdge id={id} path={path} interactionWidth={24} style={{ stroke, strokeWidth: state === 'selected' ? 2 : 1.5, opacity, transition }} />
      <path d={head} style={{ fill: stroke, opacity, transition }} pointerEvents="none" />
      {data?.label && (
        <EdgeLabelRenderer>
          <div
            className="mono pointer-events-none absolute rounded-1 border border-line-1 bg-raised px-1.5 text-ink-2"
            style={{ transform: `translate(-50%, -50%) translate(${lx}px, ${ly}px)`, fontSize: 11, lineHeight: '16px', opacity }}
            data-testid="link-label"
          >
            {data.label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
