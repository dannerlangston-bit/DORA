import { useSyncExternalStore } from 'react'
import { applyOps, invertOps, OpError, type DoraMap, type Op } from '../../shared/ops.js'

export type Layout = { positions: Record<string, { x: number; y: number }>; pinned: string[]; expanded: string[] }
export type DoraState = {
  ready: boolean
  connected: boolean
  /** the server's map with this canvas's unconfirmed edits on top: what the screen shows */
  map: DoraMap
  problems: string[]
  fileError: string | null
  layout: Layout
  workspace: { name: string; dir: string }
  /** a refused edit, said in words, cleared after a few seconds */
  notice: string | null
  canUndo: boolean
  canRedo: boolean
}

type ServerSnapshot = { rev: number; map: DoraMap; problems: string[]; error: string | null }
type Batch = { ops: Op[]; inverse: Op[] }

const EMPTY: DoraMap = { version: 1, title: '', events: [], links: [] }

/**
 * The canvas's side of the sync. Edits apply on screen at once and queue for the server, which
 * applies them to the file on disk and answers with the result. Edits from the AI arrive as
 * server-sent events; unconfirmed local edits are replayed on top so nothing flickers back.
 */
class DoraClient {
  state: DoraState = {
    ready: false, connected: false, map: EMPTY, problems: [], fileError: null,
    layout: { positions: {}, pinned: [], expanded: [] }, workspace: { name: '', dir: '' },
    notice: null, canUndo: false, canRedo: false,
  }
  private server: DoraMap = EMPTY
  private pending: Op[][] = []
  private sending = false
  private undoStack: Batch[] = []
  private redoStack: Batch[] = []
  private listeners = new Set<() => void>()
  private noticeTimer: ReturnType<typeof setTimeout> | undefined
  private layoutTimer: ReturnType<typeof setTimeout> | undefined

  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn) } }
  getState = () => this.state

  private set(patch: Partial<DoraState>) {
    this.state = { ...this.state, ...patch, canUndo: this.undoStack.length > 0, canRedo: this.redoStack.length > 0 }
    for (const fn of this.listeners) fn()
  }

  async start() {
    const res = await fetch('/api/state')
    const s = await res.json()
    this.server = s.map
    this.set({ ready: true, map: s.map, problems: s.problems, fileError: s.error, layout: s.layout, workspace: s.workspace })
    this.listen()
  }

  private listen() {
    const es = new EventSource('/api/events')
    es.addEventListener('open', () => this.set({ connected: true }))
    es.addEventListener('error', () => this.set({ connected: false }))
    es.addEventListener('state', (e) => {
      const s: ServerSnapshot & { source: string } = JSON.parse((e as MessageEvent).data)
      this.receive(s)
    })
  }

  private receive(s: ServerSnapshot) {
    this.server = s.map
    this.set({ map: this.replay(), problems: s.problems, fileError: s.error, connected: true })
  }

  /** server map + local edits not yet confirmed; any that no longer apply are dropped */
  private replay(): DoraMap {
    let map = this.server
    this.pending = this.pending.filter((ops) => {
      try { map = applyOps(map, ops); return true } catch { return false }
    })
    return map
  }

  notify(message: string) {
    clearTimeout(this.noticeTimer)
    this.set({ notice: message })
    this.noticeTimer = setTimeout(() => this.set({ notice: null }), 4000)
  }

  /**
   * Apply a batch of edits. Returns false (and says why) when Dora refuses it, e.g. a loop.
   * `record: false` keeps it out of undo history (undo and redo themselves).
   */
  apply(ops: Op[], { record = true } = {}): boolean {
    const before = this.state.map
    let after: DoraMap
    try {
      after = applyOps(before, ops)
    } catch (err) {
      if (err instanceof OpError) { this.notify(err.message); return false }
      throw err
    }
    if (record) {
      this.undoStack.push({ ops, inverse: invertOps(before, ops) })
      if (this.undoStack.length > 200) this.undoStack.shift()
      this.redoStack = []
    }
    this.pending.push(ops)
    this.set({ map: after })
    void this.flush()
    return true
  }

  /** send queued batches one at a time, so the server sees them in order */
  private async flush() {
    if (this.sending) return
    this.sending = true
    try {
      while (this.pending.length) {
        const ops = this.pending[0]
        let body: ServerSnapshot & { refused?: string }
        try {
          const res = await fetch('/api/ops', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ops }) })
          body = await res.json()
          if (body.refused) this.notify(body.refused)
        } catch {
          this.set({ connected: false })
          this.notify('Lost the connection to Dora. Is it still running in the terminal?')
          await new Promise((r) => setTimeout(r, 2000))
          continue
        }
        if (this.pending[0] === ops) this.pending.shift()
        if (body.map) this.server = body.map
        this.set({ map: this.replay(), problems: body.problems ?? this.state.problems, fileError: body.error ?? null })
      }
    } finally {
      this.sending = false
    }
  }

  undo() {
    const b = this.undoStack.pop()
    if (!b) return
    if (this.apply(b.inverse, { record: false })) this.redoStack.push(b)
    else this.notify('Can’t undo that: the map changed since')
    this.set({})
  }

  redo() {
    const b = this.redoStack.pop()
    if (!b) return
    if (this.apply(b.ops, { record: false })) this.undoStack.push(b)
    this.set({})
  }

  /**
   * Take back the newest edit as though it never happened: no redo entry. For a fresh event the
   * person clicked away from without naming it.
   */
  retract(isIt: (ops: Op[]) => boolean): boolean {
    const top = this.undoStack.at(-1)
    if (!top || !isIt(top.ops)) return false
    this.undoStack.pop()
    this.apply(top.inverse, { record: false })
    this.set({})
    return true
  }

  setLayout(patch: Partial<Layout>) {
    this.set({ layout: { ...this.state.layout, ...patch } })
    clearTimeout(this.layoutTimer)
    this.layoutTimer = setTimeout(() => {
      void fetch('/api/layout', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ layout: this.state.layout }) })
    }, 400)
  }
}

export const client = new DoraClient()

export function useDora(): DoraState {
  return useSyncExternalStore(client.subscribe, client.getState)
}
