import { useEffect, useLayoutEffect, useState } from 'react'
import { TOUR } from './tourSteps'

type Rect = { x: number; y: number; w: number; h: number }
const CARD_W = 340
const GAP = 14

/**
 * The tour (DESIGN §12): one step at a time, a ring around the thing it talks about, Back and Next.
 * Esc or Skip closes it.
 */
export function Tour({ onClose }: { onClose: () => void }) {
  const [i, setI] = useState(0)
  const [rect, setRect] = useState<Rect | null>(null)
  const step = TOUR[i]
  const last = i === TOUR.length - 1

  useLayoutEffect(() => {
    const measure = () => {
      const el = step.target ? document.querySelector(`[data-tour="${step.target}"]`) : null
      const r = el?.getBoundingClientRect()
      setRect(r && r.width ? { x: r.left, y: r.top, w: r.width, h: r.height } : null)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [step])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      e.stopPropagation()
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); if (last) onClose(); else setI((n) => n + 1) }
      else if (e.key === 'ArrowLeft') setI((n) => Math.max(0, n - 1))
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [last, onClose])

  // beside the target if there's room to its right, else above or below it; centered with no target
  const vw = window.innerWidth, vh = window.innerHeight
  let style: React.CSSProperties
  if (!rect) style = { left: vw / 2 - CARD_W / 2, top: vh / 2 - 110 }
  else if (rect.x + rect.w + GAP + CARD_W < vw - 12 && rect.x < vw / 2) style = { left: rect.x + rect.w + GAP, top: Math.min(Math.max(12, rect.y - 8), vh - 240) }
  else if (rect.y > vh / 2) style = { left: Math.min(Math.max(12, rect.x + rect.w - CARD_W), vw - CARD_W - 12), bottom: vh - rect.y + GAP }
  else style = { left: Math.min(Math.max(12, rect.x), vw - CARD_W - 12), top: rect.y + rect.h + GAP }

  return (
    <div className="fixed inset-0 z-50" data-testid="tour">
      <div className="fade-in absolute inset-0 bg-black/40" onClick={onClose} />
      {rect && (
        <div
          className="pointer-events-none absolute rounded-2 outline outline-2 outline-offset-4 outline-orange"
          style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
          aria-hidden
        />
      )}
      <div key={i} className="rise-in absolute rounded-4 border border-line-2 bg-raised p-4" style={{ ...style, width: CARD_W }} role="dialog" aria-label={step.title}>
        <div className="mono text-xs text-ink-3">{i + 1} of {TOUR.length}</div>
        <div className="mt-1 text-md font-medium text-ink-1" data-testid="tour-title">{step.title}</div>
        <p className="mt-1.5 text-sm leading-5 text-ink-2">{step.body}</p>
        <div className="mt-4 flex items-center justify-between">
          <button type="button" className="motion-fast rounded-2 px-2 py-1 text-sm text-ink-3 transition-colors hover:text-ink-1" onClick={onClose} data-testid="tour-skip">Skip</button>
          <div className="flex gap-1.5">
            {i > 0 && <button type="button" className="motion-fast h-8 rounded-2 px-3 text-sm text-ink-2 transition-colors hover:bg-sel hover:text-ink-1" onClick={() => setI(i - 1)}>Back</button>}
            <button type="button" className="motion-fast h-8 rounded-2 border border-line-2 bg-surface px-3 text-sm text-ink-1 transition-colors hover:border-line-3" onClick={() => (last ? onClose() : setI(i + 1))} data-testid="tour-next">
              {last ? 'Done' : 'Next'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
