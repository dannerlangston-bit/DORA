import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import Markdown from 'react-markdown'
import { ChevronDown, Pin } from 'lucide-react'
import { KINDS, type DoraEvent, type Kind } from '../../shared/ops.js'
import { CARD_W } from './layout'

export type Field = 'title' | 'summary' | 'details' | 'files'
export type EventNodeData = { event: DoraEvent; unplaced?: boolean }
export type EventNode = Node<EventNodeData, 'event'>

/**
 * Everything a card needs that isn't the event itself. Kept out of node data so a click or a hover
 * never rebuilds the nodes.
 */
export interface CanvasState {
  head: string | null          // dark orange: selected, and where the next link starts
  chain: Set<string>           // light orange: earlier events in the chain being linked
  hovered: string | null
  refuse: string | null        // flashes red: a link to it was refused
  pinned: Set<string>
  expanded: Set<string>
  editing: { id: string; field: Field } | null
  workspaceDir: string
  startEdit: (id: string, field: Field) => void
  commitEdit: (id: string, field: Field, value: string) => void
  cancelEdit: (id: string, field: Field) => void
  toggleExpanded: (id: string) => void
  setKind: (id: string, kind: Kind) => void
  /** the open field registers how to save itself, so a click elsewhere can save it first */
  registerEditor: (commit: (() => void) | null, owner: object) => void
}
export const CanvasContext = createContext<CanvasState | null>(null)
const useCanvas = () => useContext(CanvasContext)!

const stop = (e: { stopPropagation: () => void }) => e.stopPropagation()

/**
 * An event (DESIGN §4). Collapsed it is the snapshot: kind, title, a plain summary, how many files.
 * The Details dropdown opens the in-depth explanation and the files.
 */
export function EventNodeView({ id, data }: NodeProps<EventNode>) {
  const c = useCanvas()
  const e = data.event
  const state = c.head === id ? 'head' : c.chain.has(id) ? 'chain' : undefined
  const open = c.expanded.has(id)
  const hovered = c.hovered === id && !state
  const editingHere = c.editing?.id === id
  return (
    <div
      className={`card relative rounded-3 border ${hovered ? 'border-line-2 bg-hover' : 'border-line-1 bg-surface'} ${data.unplaced ? 'opacity-0' : 'card-in'}`}
      style={{ width: CARD_W }}
      data-state={state}
      data-refuse={c.refuse === id ? '1' : undefined}
      data-event-id={id}
      data-testid="event"
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="px-3.5 pb-2.5 pt-3">
        <div className="flex items-center justify-between gap-2">
          <select
            className="chip nodrag cursor-pointer appearance-none bg-transparent hover:border-line-3"
            style={{ fieldSizing: 'content' } as React.CSSProperties}
            value={e.kind}
            onChange={(ev) => c.setKind(id, ev.target.value as Kind)}
            onClick={stop}
            onDoubleClick={stop}
            aria-label="Kind"
            data-testid="kind"
          >
            {KINDS.map((k) => <option key={k} value={k} className="bg-raised text-ink-1">{k}</option>)}
          </select>
          {c.pinned.has(id) && (
            <span className="text-ink-3" title="Pinned: auto-spacing leaves this card where you put it. Press T to tidy everything." data-testid="pinned">
              <Pin size={12} strokeWidth={1.5} />
            </span>
          )}
        </div>
        <FieldView id={id} field="title" value={e.title} saveOn="enter" className="mt-2 text-base font-medium text-ink-1" placeholder="Untitled" testid="title" />
        {/* always there, so selecting a card never changes its height and never moves the map */}
        <FieldView id={id} field="summary" value={e.summary} saveOn="enter" className="mt-1 text-sm text-ink-2" placeholder="Add a summary (double-click)" testid="summary" clamp />
        <div className="mt-2.5 flex items-center justify-between gap-2">
          <span className="mono text-xs text-ink-3">{e.files.length ? `${e.files.length} file${e.files.length === 1 ? '' : 's'}` : ''}</span>
          <button
            type="button"
            className={`nodrag motion-fast flex h-6 items-center gap-1 rounded-2 px-1.5 text-xs transition-colors ${open ? 'bg-sel text-ink-1' : 'text-ink-2 hover:bg-sel hover:text-ink-1'}`}
            onClick={(ev) => { ev.stopPropagation(); c.toggleExpanded(id) }}
            onDoubleClick={stop}
            aria-expanded={open}
            data-testid="details-toggle"
          >
            Details
            <ChevronDown size={14} strokeWidth={1.5} className="motion-fast transition-transform" style={{ transform: open ? 'rotate(180deg)' : undefined }} />
          </button>
        </div>
      </div>
      {open && (
        <div className="nowheel scroll max-h-[360px] border-t border-line-1 px-3.5 py-3 rise-in" data-testid="details">
          <FieldView id={id} field="details" value={e.details} saveOn="mod-enter" className="text-sm" placeholder="Double-click to explain this in depth: why it exists, how it works, what it hands off. Markdown works." testid="details-body">
            {e.details ? <div className="prose-dora"><Markdown components={{ a: ({ node: _n, ...p }) => <a {...p} target="_blank" rel="noreferrer" /> }}>{e.details}</Markdown></div> : null}
          </FieldView>
          <div className="mt-3 border-t border-line-1 pt-2.5">
            <div className="mb-1.5 text-[11px] font-medium uppercase tracking-[0.04em] text-ink-3">Files</div>
            <FieldView id={id} field="files" value={e.files.join('\n')} saveOn="mod-enter" className="mono text-xs" placeholder="Double-click to add paths, one per line" testid="files">
              {e.files.length ? (
                <ul className="flex flex-wrap gap-1">
                  {e.files.map((f) => (
                    <li key={f}>
                      <a
                        className="nodrag mono inline-block max-w-[208px] truncate rounded-1 border border-line-1 px-1.5 text-[11px] leading-4 text-ink-2 hover:border-line-3 hover:text-ink-1"
                        href={c.workspaceDir ? `vscode://file/${c.workspaceDir.replace(/\/$/, '')}/${f.replace(/^\.?\//, '')}` : undefined}
                        title={`${f} (opens in VS Code)`}
                        onClick={stop}
                      >
                        {f}
                      </a>
                    </li>
                  ))}
                </ul>
              ) : null}
            </FieldView>
          </div>
          {editingHere && (c.editing?.field === 'details' || c.editing?.field === 'files') && (
            <div className="mono mt-2 text-[11px] text-ink-3">{navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'}+Enter saves · Esc cancels</div>
          )}
        </div>
      )}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  )
}

/** A field that shows its value, and turns into a text box on double-click. */
function FieldView({ id, field, value, saveOn, className, placeholder, testid, clamp = false, children }: {
  id: string; field: Field; value: string; saveOn: 'enter' | 'mod-enter'; className: string; placeholder: string; testid: string; clamp?: boolean; children?: ReactNode
}) {
  const c = useCanvas()
  if (c.editing?.id === id && c.editing.field === field) {
    return <Editor key={`${id}:${field}`} initial={value} saveOn={saveOn} className={className} label={field} testid={`${testid}-input`}
      onCommit={(v) => c.commitEdit(id, field, v)} onCancel={() => c.cancelEdit(id, field)} register={c.registerEditor} />
  }
  return (
    <div
      className={`${className} ${clamp ? 'line-clamp-3' : ''} break-words`}
      onDoubleClick={(e) => { e.stopPropagation(); c.startEdit(id, field) }}
      title={clamp && value.length > 140 ? value : undefined}
      data-testid={testid}
    >
      {children ?? (value || <span className="text-ink-3">{placeholder}</span>)}
    </div>
  )
}

function Editor({ initial, saveOn, className, label, testid, onCommit, onCancel, register }: {
  initial: string; saveOn: 'enter' | 'mod-enter'; className: string; label: string; testid: string
  onCommit: (v: string) => void; onCancel: () => void; register: CanvasState['registerEditor']
}) {
  const [draft, setDraft] = useState(initial)
  const draftRef = useRef(initial)
  draftRef.current = draft
  const done = useRef(false)
  const ref = useRef<HTMLTextAreaElement>(null)
  const owner = useRef({})
  const fit = () => { const t = ref.current; if (t) { t.style.height = '0px'; t.style.height = `${t.scrollHeight}px` } }
  const commit = useCallback(() => { if (done.current) return; done.current = true; onCommit(draftRef.current) }, [onCommit])
  const cancel = () => { if (done.current) return; done.current = true; onCancel() }
  useEffect(() => { register(commit, owner.current) }, [commit, register])
  useEffect(() => () => register(null, owner.current), [register])
  useLayoutEffect(() => {
    // a card the canvas hasn't measured yet is hidden for a frame or two, and hidden things can't
    // take focus, so keep trying until the text box has it
    let frame = 0, tries = 0
    const focus = () => {
      const t = ref.current
      if (!t || done.current) return
      t.focus({ preventScroll: true })
      if (document.activeElement === t) { t.setSelectionRange(t.value.length, t.value.length); return }
      if (++tries < 30) frame = requestAnimationFrame(focus)
    }
    fit()
    focus()
    return () => cancelAnimationFrame(frame)
  }, [])
  return (
    <textarea
      ref={ref}
      value={draft}
      rows={1}
      aria-label={label}
      data-testid={testid}
      spellCheck
      onChange={(e) => { setDraft(e.target.value); fit() }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel() }
        else if (e.key === 'Enter' && (saveOn === 'enter' ? !e.shiftKey : e.metaKey || e.ctrlKey)) { e.preventDefault(); commit() }
      }}
      onClick={stop}
      onDoubleClick={stop}
      className={`nodrag nowheel block w-full resize-none overflow-hidden rounded-1 bg-transparent p-0 outline outline-1 outline-offset-2 outline-line-3 ${className}`}
    />
  )
}
