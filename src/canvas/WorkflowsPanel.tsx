import { useState } from 'react'
import { Play, Plus, Trash2, X } from 'lucide-react'
import type { DoraWorkflow } from '../../shared/ops.js'

/**
 * Workflows (DESIGN §6b): the paths customers take through the map. Click one to light its path;
 * play it to walk through it; make a new one by naming it and clicking its steps in order.
 */
export function WorkflowsPanel(props: {
  workflows: DoraWorkflow[]
  focused: string | null
  recording: { title: string; steps: string[] } | null
  onFocus: (id: string | null) => void
  onPresent: (id: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
  onStartRecording: (title: string) => void
  onFinishRecording: () => void
  onCancelRecording: () => void
  onClose: () => void
}) {
  const [naming, setNaming] = useState<string | null>(null)
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null)
  return (
    <div className="rise-in absolute right-3 top-3 z-30 flex max-h-[calc(100%-80px)] w-[300px] flex-col rounded-3 border border-line-1 bg-raised" data-testid="workflows-panel">
      <div className="flex items-center justify-between px-3 pb-1 pt-2.5">
        <span className="text-[13px] font-medium uppercase tracking-[0.04em] text-ink-2">Workflows</span>
        <button type="button" className="motion-fast rounded-2 p-1 text-ink-3 transition-colors hover:bg-sel hover:text-ink-1" onClick={props.onClose} aria-label="Close workflows">
          <X size={14} strokeWidth={1.5} />
        </button>
      </div>
      <div className="px-3 pb-2 text-xs text-ink-3">A workflow is one path through the map, like how a customer gets from sign-up to done.</div>

      {props.recording ? (
        <div className="mx-2 mb-2 rounded-2 border border-orange-line px-2.5 py-2" data-testid="recording">
          <div className="text-sm text-ink-1">{props.recording.title}</div>
          <div className="mt-0.5 text-xs text-ink-2">Click the steps in order. Missing links are added for you.</div>
          <div className="mono mt-1 text-xs text-ink-3">{props.recording.steps.length} step{props.recording.steps.length === 1 ? '' : 's'}</div>
          <div className="mt-2 flex gap-1.5">
            <button type="button" className="motion-fast h-7 rounded-2 border border-line-2 bg-surface px-2.5 text-sm text-ink-1 transition-colors hover:border-line-3 disabled:opacity-40" disabled={props.recording.steps.length < 2} onClick={props.onFinishRecording} data-testid="recording-done">Done</button>
            <button type="button" className="motion-fast h-7 rounded-2 px-2.5 text-sm text-ink-2 transition-colors hover:bg-sel hover:text-ink-1" onClick={props.onCancelRecording}>Cancel</button>
          </div>
        </div>
      ) : naming !== null ? (
        <form className="mx-2 mb-2" onSubmit={(e) => { e.preventDefault(); if (naming.trim()) { props.onStartRecording(naming.trim()); setNaming(null) } }}>
          <input
            autoFocus
            value={naming}
            onChange={(e) => setNaming(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') setNaming(null) }}
            placeholder="Name it, e.g. New customer signs up"
            aria-label="Workflow name"
            className="h-8 w-full rounded-2 border border-line-2 bg-surface px-2.5 text-sm placeholder:text-ink-3 focus:border-line-3"
            data-testid="workflow-name"
          />
          <div className="mt-1 px-0.5 text-xs text-ink-3">Press Enter, then click its steps on the map.</div>
        </form>
      ) : (
        <button type="button" className="motion-fast mx-2 mb-2 flex h-8 items-center gap-1.5 rounded-2 px-2 text-sm text-ink-2 transition-colors hover:bg-sel hover:text-ink-1" onClick={() => setNaming('')} data-testid="new-workflow">
          <Plus size={14} strokeWidth={1.5} /> New workflow
        </button>
      )}

      <ul className="scroll min-h-0 flex-1 border-t border-line-1 px-1.5 py-1.5" aria-label="Workflows">
        {props.workflows.length === 0 && <li className="px-1.5 py-2 text-xs text-ink-3">No workflows yet.</li>}
        {props.workflows.map((w) => {
          const on = props.focused === w.id
          return (
            <li key={w.id} className={`group rounded-2 px-1.5 py-1.5 ${on ? 'bg-sel' : 'hover:bg-hover'}`} data-workflow-id={w.id}>
              <div className="flex items-center gap-1">
                {renaming?.id === w.id ? (
                  <input
                    autoFocus
                    value={renaming.title}
                    onChange={(e) => setRenaming({ id: w.id, title: e.target.value })}
                    onBlur={() => { if (renaming.title.trim()) props.onRename(w.id, renaming.title.trim()); setRenaming(null) }}
                    onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') setRenaming(null) }}
                    className="h-6 min-w-0 flex-1 rounded-1 bg-transparent text-sm text-ink-1 outline outline-1 outline-offset-2 outline-line-3"
                    aria-label="Workflow name"
                  />
                ) : (
                  <button
                    type="button"
                    className="min-w-0 flex-1 truncate text-left text-sm text-ink-1"
                    onClick={() => props.onFocus(on ? null : w.id)}
                    onDoubleClick={() => setRenaming({ id: w.id, title: w.title })}
                    title="Click to light up its path. Double-click to rename."
                    aria-pressed={on}
                    data-testid="workflow-item"
                  >
                    {w.title || 'Untitled workflow'}
                  </button>
                )}
                <span className="mono shrink-0 text-xs text-ink-3">{w.steps.length}</span>
                <button type="button" className="motion-fast shrink-0 rounded-2 p-1 text-ink-2 transition-colors hover:bg-sel hover:text-ink-1" onClick={() => props.onPresent(w.id)} aria-label={`Present ${w.title}`} title="Walk through it" data-testid="workflow-present">
                  <Play size={13} strokeWidth={1.5} />
                </button>
                <button type="button" className="motion-fast shrink-0 rounded-2 p-1 text-ink-3 opacity-0 transition-colors hover:bg-sel hover:text-ink-1 group-hover:opacity-100 focus:opacity-100" onClick={() => props.onDelete(w.id)} aria-label={`Delete ${w.title}`} title="Delete this workflow (the cards stay)">
                  <Trash2 size={13} strokeWidth={1.5} />
                </button>
              </div>
              {w.summary && <div className="mt-0.5 line-clamp-2 pr-12 text-xs text-ink-3">{w.summary}</div>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
