import Markdown from 'react-markdown'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { KIND_LABEL } from '../../shared/kinds.js'
import type { DoraEvent } from '../../shared/ops.js'
import { KIND_ICON } from './kindIcons'

/**
 * Present (DESIGN §6c): one step at a time, the camera following. The step's words show here
 * instead of opening the card, so the map never re-spaces mid-presentation.
 */
export function Present({ title, steps, index, onGo, onClose }: { title: string; steps: DoraEvent[]; index: number; onGo: (i: number) => void; onClose: () => void }) {
  const e = steps[index]
  if (!e) return null
  const Icon = KIND_ICON[e.kind]
  const who = [e.actor, e.system].filter(Boolean).join(' · ')
  return (
    <div className="rise-in absolute bottom-3 left-1/2 z-30 flex max-h-[45%] w-[min(520px,calc(100%-24px))] -translate-x-1/2 flex-col rounded-4 border border-line-1 bg-raised" role="dialog" aria-label={`Presenting ${title}`} data-testid="present">
      <div className="flex items-center justify-between gap-2 border-b border-line-1 px-4 py-2">
        <span className="truncate text-xs text-ink-3">{title}</span>
        <span className="mono shrink-0 text-xs text-ink-2" data-testid="present-step">Step {index + 1} of {steps.length}</span>
      </div>
      <div className="scroll min-h-0 flex-1 px-4 py-3">
        <div className="flex items-center gap-1.5 text-ink-2">
          <Icon size={14} strokeWidth={1.5} />
          <span className="chip">{KIND_LABEL[e.kind]}</span>
        </div>
        <div className="mt-2 text-md font-medium text-ink-1" data-testid="present-title">{e.title || 'Untitled'}</div>
        {who && <div className="mono mt-0.5 text-xs text-ink-3">{who}</div>}
        {e.summary && <div className="mt-1.5 text-sm text-ink-1">{e.summary}</div>}
        {e.details && <div className="prose-dora mt-3 border-t border-line-1 pt-3"><Markdown>{e.details}</Markdown></div>}
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-line-1 px-2 py-1.5">
        <button type="button" className="motion-fast flex h-7 items-center gap-1 rounded-2 px-2 text-sm text-ink-2 transition-colors hover:bg-sel hover:text-ink-1" onClick={onClose} data-testid="present-close">
          <X size={14} strokeWidth={1.5} /> Close
        </button>
        <div className="flex items-center gap-1">
          <button type="button" className="motion-fast flex h-7 items-center gap-1 rounded-2 px-2 text-sm text-ink-2 transition-colors hover:bg-sel hover:text-ink-1 disabled:opacity-40" disabled={index === 0} onClick={() => onGo(index - 1)} data-testid="present-back">
            <ChevronLeft size={14} strokeWidth={1.5} /> Back
          </button>
          <button type="button" className="motion-fast flex h-7 items-center gap-1 rounded-2 border border-line-2 bg-surface px-2.5 text-sm text-ink-1 transition-colors hover:border-line-3 disabled:opacity-40" disabled={index === steps.length - 1} onClick={() => onGo(index + 1)} data-testid="present-next">
            Next <ChevronRight size={14} strokeWidth={1.5} />
          </button>
        </div>
      </div>
    </div>
  )
}
