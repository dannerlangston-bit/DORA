import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { applyNodeChanges, ReactFlow, ReactFlowProvider, useReactFlow, ViewportPortal, type NodeChange, type Viewport } from '@xyflow/react'
import '@xyflow/react/dist/base.css'
import { KIND_LABEL, type Kind } from '../../shared/kinds.js'
import { findEvent, findLink, findWorkflow, makeEvent, newEventId, newWorkflowId, type DoraEvent, type Op } from '../../shared/ops.js'
import { client, useDora } from '../lib/client'
import { placeBlueprint, type LaneBand } from './blueprint'
import { CanvasContext, EventNodeView, type CanvasState, type EventNode, type Field } from './EventNode'
import { isTyping, useLive, useModifier } from './keys'
import { Lanes } from './Lanes'
import { CARD_W, DEFAULT_H, GRID, placeEvents, snap, type XY } from './layout'
import { LeftRail, TOOL_KEYS, type Tool } from './LeftRail'
import { LinkEdgeView, type LinkEdge, type LinkState } from './LinkEdge'
import { Pattern } from './Pattern'
import { Present } from './Present'
import { SelectionBar } from './SelectionBar'
import { Toolbar } from './Toolbar'
import { WorkflowsPanel } from './WorkflowsPanel'

const nodeTypes = { event: EventNodeView }
const edgeTypes = { link: LinkEdgeView }
const MOVE_MS = 300

type Selection = { kind: 'events'; ids: string[] } | { kind: 'link'; from: string; to: string } | null
type Armed = { from: string | null } | null
type CommitResult = 'saved' | 'discarded' | 'unchanged'
type Presenting = { workflow: string | null; index: number }
type Marquee = { x0: number; y0: number; x1: number; y1: number; additive: boolean; base: string[] }

const ease = (t: number) => 1 - Math.pow(1 - t, 3)
const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const viewportKey = (dir: string) => `dora:viewport:${dir}`
const stored = <T,>(key: string, fallback: T): T => { try { const v = localStorage.getItem(key); return v ? (JSON.parse(v) as T) : fallback } catch { return fallback } }
const store = (key: string, v: unknown) => { try { localStorage.setItem(key, JSON.stringify(v)) } catch { /* private window */ } }

export function Canvas({ onHint, onTour }: { onHint: (hint: string | null) => void; onTour: () => void }) {
  return (
    <ReactFlowProvider>
      <Board onHint={onHint} onTour={onTour} />
    </ReactFlowProvider>
  )
}

function Board({ onHint, onTour }: { onHint: (hint: string | null) => void; onTour: () => void }) {
  const { map, layout, workspace } = useDora()
  const rf = useReactFlow<EventNode, LinkEdge>()
  const view = layout.view
  const [nodes, setNodes] = useState<EventNode[]>([])
  const nodesRef = useRef(nodes)
  nodesRef.current = nodes

  const [tool, setTool, toolRef] = useLive<Tool>('select')
  const [boxKind, setBoxKindState, boxKindRef] = useLive<Kind>(stored<Kind>('dora:box-kind', 'action'))
  const [sel, setSel, selRef] = useLive<Selection>(null)
  const [chain, setChain, chainRef] = useLive<string[]>([])
  const [armed, setArmed, armedRef] = useLive<Armed>(null)
  const [editing, setEditing, editingRef] = useLive<{ id: string; field: Field } | null>(null)
  const [recording, setRecording, recordingRef] = useLive<{ title: string; steps: string[] } | null>(null)
  const [presenting, setPresenting, presentingRef] = useLive<Presenting | null>(null)
  const [focusedWf, setFocusedWf] = useState<string | null>(null)
  const [workflowsOpen, setWorkflowsOpen] = useState(false)
  const [hovered, setHovered] = useState<string | null>(null)
  const [refuse, setRefuse] = useState<string | null>(null)
  const [cursor, setCursor] = useState<XY | null>(null)
  const [dragging, setDragging] = useState(false)
  const [tidyRun, setTidyRun] = useState(0)
  const [shiftDown, setShiftDown] = useState(false)
  const [marquee, setMarquee] = useState<Marquee | null>(null)
  const [lanes, setLanes] = useState<{ bands: LaneBand[]; width: number } | null>(null)

  /** events made on the canvas and not yet named; clicking away from one unnamed removes it */
  const fresh = useRef(new Set<string>())
  /** where an event made on the canvas first appears, before auto-spacing moves it */
  const spawnAt = useRef(new Map<string, XY>())
  const editor = useRef<{ commit: () => void; owner: object } | null>(null)
  const commitMod = useRef(false)
  const lastCommit = useRef<CommitResult | null>(null)
  const discardedAt = useRef(-Infinity)
  const lastContextClick = useRef(-Infinity)
  const marqueeRef = useRef<Marquee | null>(null)
  const marqueeEndedAt = useRef(-Infinity)
  const anim = useRef(0)
  const fitted = useRef(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  const pinned = useMemo(() => new Set(layout.pinned), [layout.pinned])
  const expanded = useMemo(() => new Set(layout.expanded), [layout.expanded])
  const selIds = () => (selRef.current?.kind === 'events' ? selRef.current.ids : [])
  const primary = () => selIds().at(-1) ?? null
  const selectOnly = (id: string | null) => setSel(id ? { kind: 'events', ids: [id] } : null)
  const setBoxKind = (k: Kind) => { setBoxKindState(k); store('dora:box-kind', k) }

  const mod = useModifier({
    onTap: () => {
      if (armedRef.current) { setArmed(null); return }
      if (client.getState().map.events.length < 2 || presentingRef.current || recordingRef.current) return
      setArmed({ from: null })
    },
    onRelease: () => { if (toolRef.current !== 'chain') setChain([]) },
  })

  useEffect(() => {
    const down = (e: KeyboardEvent) => { if (e.key === 'Shift') setShiftDown(true) }
    const up = (e: KeyboardEvent) => { if (e.key === 'Shift') setShiftDown(false) }
    const blur = () => setShiftDown(false)
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', blur)
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); window.removeEventListener('blur', blur) }
  }, [])

  // ---- nodes follow the map: keep each card's position and measured size, swap in the new event
  useEffect(() => {
    setNodes((prev) => {
      const old = new Map(prev.map((n) => [n.id, n]))
      return map.events.map((e): EventNode => {
        const o = old.get(e.id)
        if (o) return o.data.event === e ? o : { ...o, data: { ...o.data, event: e } }
        const spawn = spawnAt.current.get(e.id)
        spawnAt.current.delete(e.id)
        const l = client.getState().layout
        const position = spawn ?? (l.view === 'flow' ? l.positions[e.id] : undefined)
        // a starting size lets React Flow show the card at once instead of hiding it until measured,
        // so a new card's title box can take the first keystroke
        return { id: e.id, type: 'event', position: position ?? { x: 0, y: 0 }, initialWidth: CARD_W, initialHeight: DEFAULT_H, data: { event: e, unplaced: !position } }
      })
    })
    // anything that no longer exists leaves the selection, the chain and the workflow being recorded
    const ids = new Set(map.events.map((e) => e.id))
    if (selRef.current?.kind === 'events' && selRef.current.ids.some((id) => !ids.has(id))) {
      const keep = selRef.current.ids.filter((id) => ids.has(id))
      setSel(keep.length ? { kind: 'events', ids: keep } : null)
    }
    if (chainRef.current.some((id) => !ids.has(id))) setChain((c) => c.filter((id) => ids.has(id)))
    if (recordingRef.current?.steps.some((id) => !ids.has(id))) setRecording((r) => (r ? { ...r, steps: r.steps.filter((id) => ids.has(id)) } : r))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map.events])

  useEffect(() => { if (focusedWf && !findWorkflow(map, focusedWf)) setFocusedWf(null) }, [map, focusedWf])

  // ---- auto-spacing (DESIGN §7). Re-runs when the story's shape, a card's height, the pins or the view change.
  const structureKey = JSON.stringify([
    view,
    map.events.map((e) => (view === 'blueprint' ? `${e.id}:${e.kind}` : e.id)),
    map.links.map((l) => `${l.from}>${l.to}`),
    view === 'flow' ? [layout.pinned, layout.placed] : null,
    tidyRun,
  ])
  const heightsNow = nodes.map((n) => Math.round((n.measured?.height ?? 0) / 4)).join(',')
  const heldHeights = useRef(heightsNow)
  if (!editing) heldHeights.current = heightsNow // nothing moves under someone typing
  const allMeasured = nodes.length === map.events.length && nodes.every((n) => n.measured?.height)

  useEffect(() => {
    if (dragging || !allMeasured) return
    let cancelled = false
    const current = nodesRef.current
    const heights: Record<string, number> = {}
    for (const n of current) heights[n.id] = n.measured?.height ?? DEFAULT_H
    const { map: m, layout: l } = client.getState()
    const ids = current.map((n) => n.id)
    if (l.view === 'blueprint') {
      const kinds = Object.fromEntries(m.events.map((e) => [e.id, e.kind]))
      void placeBlueprint({ ids, kinds, links: m.links, heights }).then((bp) => {
        if (cancelled) return
        setLanes({ bands: bp.lanes, width: bp.width })
        moveTo(bp.positions, false)
      })
    } else {
      // the saved Flow positions are the truth (the cards may be showing Blueprint right now)
      const positions: Record<string, XY> = {}
      for (const n of current) {
        const saved = l.positions[n.id]
        if (saved) positions[n.id] = saved
        else if (!n.data.unplaced) positions[n.id] = n.position
      }
      const fixed = new Set([...l.pinned, ...l.placed])
      void placeEvents({ ids, links: m.links, heights, positions, pinned: fixed }).then((target) => {
        if (cancelled) return
        setLanes(null)
        moveTo(target, true)
      })
    }
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structureKey, heldHeights.current, dragging, allMeasured])

  function moveTo(target: Record<string, XY>, save: boolean) {
    cancelAnimationFrame(anim.current)
    const from = new Map(nodesRef.current.map((n) => [n.id, n.position]))
    const moving = nodesRef.current.some((n) => { const t = target[n.id]; return t && !n.data.unplaced && (t.x !== n.position.x || t.y !== n.position.y) })
    const finish = () => {
      setNodes((ns) => ns.map((n) => (target[n.id] ? { ...n, position: target[n.id], data: n.data.unplaced ? { ...n.data, unplaced: false } : n.data } : n)))
      if (save) {
        const saved = client.getState().layout.positions
        if (Object.keys(target).some((id) => saved[id]?.x !== target[id].x || saved[id]?.y !== target[id].y) || Object.keys(saved).some((id) => !target[id])) {
          client.setLayout({ positions: target })
        }
      }
      if (!fitted.current) firstView()
    }
    if (!moving || reducedMotion()) { finish(); return }
    // cards with nowhere yet (the AI just wrote them) appear in place; the rest glide
    setNodes((ns) => ns.map((n) => (n.data.unplaced && target[n.id] ? { ...n, position: target[n.id], data: { ...n.data, unplaced: false } } : n)))
    const start = performance.now()
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / MOVE_MS)
      if (k >= 1) { finish(); return }
      const e = ease(k)
      setNodes((ns) => ns.map((n) => {
        const to = target[n.id], f = from.get(n.id)
        if (!to || !f || n.data.unplaced) return n
        return { ...n, position: { x: f.x + (to.x - f.x) * e, y: f.y + (to.y - f.y) * e } }
      }))
      anim.current = requestAnimationFrame(step)
    }
    anim.current = requestAnimationFrame(step)
  }

  function firstView() {
    fitted.current = true
    const saved = stored<Viewport | null>(viewportKey(workspace.dir), null)
    if (saved && typeof saved.zoom === 'number') void rf.setViewport(saved)
    else if (nodesRef.current.length) void rf.fitView({ padding: 0.2, maxZoom: 1 })
  }
  useEffect(() => { if (!map.events.length && !fitted.current) firstView() }) // empty map: nothing to wait for

  // ---- edits
  function flashRefused(id: string) {
    setRefuse(id)
    setTimeout(() => setRefuse((r) => (r === id ? null : r)), 600)
  }

  /** Link from → to. "exists" when they already are; "refused" (said why) when Dora won't. */
  function link(from: string, to: string): 'linked' | 'exists' | 'refused' {
    if (findLink(client.getState().map, from, to)) return 'exists'
    if (client.apply([{ op: 'addLink', link: { from, to, label: '' } }])) return 'linked'
    flashRefused(to)
    return 'refused'
  }

  function newKind(): Kind {
    const k = boxKindRef.current
    return client.getState().map.events.length === 0 && k === 'action' ? 'start' : k
  }

  /** a new card, named straight away; `placed` keeps it where it was put instead of auto-spacing it */
  function createEvent(at: XY, from: string | null, placed: boolean): string | null {
    const m = client.getState().map
    const id = newEventId(m)
    const ops: Op[] = [{ op: 'addEvent', event: makeEvent({ id, kind: newKind() }) }]
    if (from) ops.push({ op: 'addLink', link: { from, to: id, label: '' } })
    const spot = { x: snap(at.x - CARD_W / 2), y: snap(at.y - 24) }
    spawnAt.current.set(id, spot)
    if (!client.apply(ops)) { spawnAt.current.delete(id); return null }
    if (placed) {
      const l = client.getState().layout
      client.setLayout({ placed: [...l.placed, id], positions: { ...l.positions, [id]: spot } })
    }
    fresh.current.add(id)
    selectOnly(id)
    setEditing({ id, field: 'title' })
    return id
  }

  /** a new step in the middle of a link: from → new → to, and the same in every workflow that had from → to */
  function insertBetween(from: string, to: string) {
    const m = client.getState().map
    const old = findLink(m, from, to)
    if (!old) return
    const id = newEventId(m)
    const ops: Op[] = [
      { op: 'deleteLink', from, to },
      { op: 'addEvent', event: makeEvent({ id, kind: boxKindRef.current }) },
      { op: 'addLink', link: { from, to: id, label: old.label } },
      { op: 'addLink', link: { from: id, to, label: '' } },
    ]
    for (const w of m.workflows) {
      const i = w.steps.indexOf(from)
      if (i >= 0 && w.steps[i + 1] === to) ops.push({ op: 'updateWorkflow', id: w.id, patch: { steps: [...w.steps.slice(0, i + 1), id, ...w.steps.slice(i + 1)] } })
    }
    const a = nodesRef.current.find((n) => n.id === from), b = nodesRef.current.find((n) => n.id === to)
    if (a && b) spawnAt.current.set(id, { x: snap((a.position.x + b.position.x) / 2), y: snap((a.position.y + b.position.y) / 2) })
    if (!client.apply(ops)) { spawnAt.current.delete(id); return }
    fresh.current.add(id)
    selectOnly(id)
    setEditing({ id, field: 'title' })
  }

  function forget(ids: string[]) {
    const gone = new Set(ids)
    for (const id of ids) fresh.current.delete(id)
    if (selRef.current?.kind === 'events') {
      const keep = selRef.current.ids.filter((id) => !gone.has(id))
      setSel(keep.length ? { kind: 'events', ids: keep } : null)
    }
    setChain((c) => c.filter((x) => !gone.has(x)))
    const l = client.getState().layout
    if ([...l.pinned, ...l.placed, ...l.expanded].some((id) => gone.has(id))) {
      client.setLayout({ pinned: l.pinned.filter((x) => !gone.has(x)), placed: l.placed.filter((x) => !gone.has(x)), expanded: l.expanded.filter((x) => !gone.has(x)) })
    }
  }

  /** remove an unnamed new event as though it was never made (no undo entry when possible) */
  function discard(id: string) {
    discardedAt.current = performance.now()
    const made = (ops: Op[]) => ops.some((o) => o.op === 'addEvent' && o.event.id === id)
    if (!client.retract(made)) client.apply([{ op: 'deleteEvent', id }])
    forget([id])
  }

  function deleteEvents(ids: string[]) {
    if (ids.length && client.apply(ids.map((id): Op => ({ op: 'deleteEvent', id })))) forget(ids)
  }

  const commitEdit = (id: string, field: Field, raw: string): void => {
    setEditing(null)
    const e = findEvent(client.getState().map, id)
    const wasFresh = fresh.current.has(id)
    if (field === 'title') fresh.current.delete(id)
    let result: CommitResult = 'unchanged'
    if (e) {
      if (field === 'title' && !raw.trim() && wasFresh && !(mod.current.held || commitMod.current || toolRef.current === 'chain')) {
        discard(id)
        result = 'discarded'
      } else {
        const value = field === 'files' ? raw.split('\n').map((s) => s.trim()).filter(Boolean)
          : field === 'details' ? raw.replace(/\s+$/, '') : raw.replace(/\s+/g, ' ').trim()
        if (JSON.stringify(e[field]) !== JSON.stringify(value)) {
          const m = client.getState().map
          const ops: Op[] = [{ op: 'updateEvent', id, patch: { [field]: value } }]
          // the first box you name is the project: it names the map, unless you've named the map already
          const unnamed = !m.title || m.title === workspace.name || m.title === 'Untitled project'
          if (field === 'title' && value && m.events.length === 1 && unnamed) ops.push({ op: 'setTitle', title: value as string })
          client.apply(ops)
          result = 'saved'
        }
      }
    }
    lastCommit.current = result
  }

  const cancelEdit = (id: string, field: Field) => {
    setEditing(null)
    const e = findEvent(client.getState().map, id)
    if (field === 'title' && fresh.current.has(id) && e && !e.title) { discard(id); lastCommit.current = 'discarded' }
    else lastCommit.current = 'unchanged'
  }

  /** save whatever field is open before a click acts; tells the click if a new card went away */
  function saveOpenField(withMod: boolean): CommitResult | null {
    if (!editor.current) return null
    lastCommit.current = null
    commitMod.current = withMod
    editor.current.commit()
    commitMod.current = false
    return lastCommit.current
  }

  // ---- many cards at once (DESIGN §5b)
  function setPinned(ids: string[], pin: boolean) {
    const l = client.getState().layout
    const set = new Set(ids)
    const positions = { ...l.positions }
    if (l.view === 'flow') for (const n of nodesRef.current) if (set.has(n.id)) positions[n.id] = { x: snap(n.position.x), y: snap(n.position.y) }
    client.setLayout(pin
      ? { pinned: [...new Set([...l.pinned, ...ids])], placed: l.placed.filter((x) => !set.has(x)), positions }
      // unpinned cards stay where they are: they become placed, which Tidy can re-space
      : { pinned: l.pinned.filter((x) => !set.has(x)), placed: [...new Set([...l.placed, ...ids])], positions })
  }
  function updateMany(ids: string[], patch: (e: DoraEvent) => Partial<DoraEvent> | null) {
    const m = client.getState().map
    const ops: Op[] = []
    for (const id of ids) {
      const e = findEvent(m, id)
      const p = e && patch(e)
      if (p) ops.push({ op: 'updateEvent', id, patch: p })
    }
    if (ops.length) client.apply(ops)
  }
  function setExpanded(ids: string[], open: boolean) {
    const l = client.getState().layout
    const set = new Set(ids)
    client.setLayout({ expanded: open ? [...new Set([...l.expanded, ...ids])] : l.expanded.filter((x) => !set.has(x)) })
  }

  // ---- workflows (DESIGN §6b)
  function startRecording(title: string) {
    saveOpenField(false)
    setArmed(null); setChain([]); setSel(null); setTool('select'); setFocusedWf(null); setPresenting(null)
    setRecording({ title, steps: [] })
  }
  function recordStep(id: string) {
    const r = recordingRef.current
    if (!r) return
    if (r.steps.at(-1) === id) { setRecording({ ...r, steps: r.steps.slice(0, -1) }); return } // click the last one again to take it back
    if (r.steps.includes(id)) { client.notify('That card is already in this workflow'); flashRefused(id); return }
    const prev = r.steps.at(-1)
    if (prev && link(prev, id) === 'refused') return
    setRecording({ ...r, steps: [...r.steps, id] })
  }
  function finishRecording() {
    const r = recordingRef.current
    if (!r || r.steps.length < 2) return
    const id = newWorkflowId(client.getState().map)
    if (client.apply([{ op: 'addWorkflow', workflow: { id, title: r.title, summary: '', steps: r.steps } }])) {
      setRecording(null)
      setFocusedWf(id)
    }
  }

  // ---- present (DESIGN §6c)
  /** the story in reading order: follow the links, left to right, top to bottom */
  function readingOrder(): string[] {
    const m = client.getState().map
    const pos = new Map(nodesRef.current.map((n) => [n.id, n.position]))
    const indeg = new Map(m.events.map((e) => [e.id, 0]))
    for (const l of m.links) indeg.set(l.to, (indeg.get(l.to) ?? 0) + 1)
    const byPlace = (a: string, b: string) => (pos.get(a)?.x ?? 0) - (pos.get(b)?.x ?? 0) || (pos.get(a)?.y ?? 0) - (pos.get(b)?.y ?? 0)
    const ready = m.events.filter((e) => !indeg.get(e.id)).map((e) => e.id).sort(byPlace)
    const order: string[] = []
    while (ready.length) {
      const id = ready.shift()!
      order.push(id)
      for (const l of m.links) {
        if (l.from !== id) continue
        indeg.set(l.to, (indeg.get(l.to) ?? 1) - 1)
        if (indeg.get(l.to) === 0) { ready.push(l.to); ready.sort(byPlace) }
      }
    }
    for (const e of m.events) if (!order.includes(e.id)) order.push(e.id) // anything in a loop, at the end
    return order
  }
  const presentOrder = useMemo(() => {
    if (!presenting) return []
    const w = presenting.workflow ? findWorkflow(map, presenting.workflow) : null
    return w ? w.steps : readingOrder()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presenting?.workflow, !!presenting, map])
  const presentOrderRef = useRef(presentOrder)
  presentOrderRef.current = presentOrder
  function present(workflow: string | null) {
    saveOpenField(false)
    const m = client.getState().map
    if (!m.events.length) return
    const wf = workflow ?? focusedWf ?? m.workflows[0]?.id ?? null
    setRecording(null); setArmed(null); setChain([]); setSel(null)
    if (wf) setFocusedWf(wf)
    setPresenting({ workflow: wf, index: 0 })
  }
  function presentGo(index: number) {
    setPresenting((p) => (p ? { ...p, index: Math.max(0, Math.min(index, presentOrderRef.current.length - 1)) } : p))
  }
  useEffect(() => {
    if (!presenting) return
    const id = presentOrder[presenting.index]
    const n = nodesRef.current.find((x) => x.id === id)
    if (!n) return
    const zoom = Math.max(rf.getZoom(), 0.9)
    const h = n.measured?.height ?? DEFAULT_H
    const below = ((wrapRef.current?.clientHeight ?? 800) * 0.18) / zoom // keep the card above the Present panel
    void rf.setCenter(n.position.x + CARD_W / 2, n.position.y + h / 2 + below, { zoom, duration: 400 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presenting?.index, presenting?.workflow, presentOrder])

  // ---- clicks (DESIGN §5, §6)
  function clickEvent(id: string, e: { mod: boolean; shift: boolean }) {
    if (editingRef.current && editingRef.current.id !== id) saveOpenField(e.mod)
    if (!findEvent(client.getState().map, id)) return
    if (recordingRef.current) { recordStep(id); return }
    if (presentingRef.current) {
      const i = presentOrderRef.current.indexOf(id)
      if (i >= 0) presentGo(i)
      return
    }
    const a = armedRef.current
    if (a) {
      const from = a.from ?? primary()
      if (!from) { setArmed({ from: id }); selectOnly(id); return }
      setArmed(null)
      if (from !== id && link(from, id) === 'exists') { setSel({ kind: 'link', from, to: id }); return }
      selectOnly(id)
      return
    }
    if (e.mod || toolRef.current === 'chain') {
      const c = chainRef.current
      const head = c.at(-1) ?? primary()
      if (!head || head === id) { if (!c.length) setChain([id]); selectOnly(id); return }
      if (link(head, id) === 'refused') return
      setChain([...(c.length ? c : [head]).filter((x) => x !== id), id])
      selectOnly(id)
      return
    }
    if (toolRef.current === 'pan') return
    if (e.shift) {
      const ids = selIds()
      setSel(ids.includes(id) ? (ids.length > 1 ? { kind: 'events', ids: ids.filter((x) => x !== id) } : null) : { kind: 'events', ids: [...ids, id] })
      return
    }
    const ids = selIds()
    selectOnly(ids.length === 1 && ids[0] === id ? null : id)
  }

  function clickPane(at: XY, e: { mod: boolean }) {
    // pressing the mouse may already have saved (and removed) an open card before this click arrived
    if (saveOpenField(e.mod) === 'discarded' || performance.now() - discardedAt.current < 500) return
    if (performance.now() - marqueeEndedAt.current < 250) return
    if (recordingRef.current || presentingRef.current) return
    if (armedRef.current) { setArmed(null); return }
    if (e.mod || toolRef.current === 'chain') {
      const c = chainRef.current
      const head = c.at(-1) ?? primary()
      const id = createEvent(at, head, !head)
      if (id) setChain([...(c.length ? c : head ? [head] : []), id])
      return
    }
    if (toolRef.current === 'box') {
      const from = selIds().length === 1 ? primary() : null
      createEvent(at, from, !from)
      return
    }
    setSel(null)
  }

  function doubleClickPane(at: XY) {
    if (toolRef.current !== 'select' || recordingRef.current || presentingRef.current || client.getState().layout.view !== 'flow') return
    createEvent(at, null, true)
  }

  const flowPoint = (e: { clientX: number; clientY: number }) => rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
  // On a Mac, Ctrl-click is the right-click menu. Dora takes it as a Ctrl-click instead; the click
  // event that may follow is ignored so it doesn't count twice.
  const fromContextMenu = (e: React.MouseEvent | MouseEvent) => { e.preventDefault(); lastContextClick.current = performance.now() }
  const echo = () => performance.now() - lastContextClick.current < 400
  const onEmpty = (target: EventTarget | null) => target instanceof Element && !!target.closest('.react-flow__pane') && !target.closest('.react-flow__node, .react-flow__edge')

  // ---- area selection: the Area tool, or Shift-drag in Select
  function onPointerDown(e: React.PointerEvent) {
    if (e.button !== 0 || !onEmpty(e.target) || recordingRef.current || presentingRef.current) return
    const t = toolRef.current
    if (!(t === 'area' || (t === 'select' && e.shiftKey))) return
    saveOpenField(false)
    const m: Marquee = { x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY, additive: e.shiftKey, base: selIds() }
    marqueeRef.current = m
    setMarquee(m)
    wrapRef.current?.setPointerCapture(e.pointerId)
  }
  function onPointerMove(e: React.PointerEvent) {
    if (armedRef.current || chainRef.current.length || toolRef.current === 'chain') setCursor(flowPoint(e))
    const m = marqueeRef.current
    if (!m) return
    const next = { ...m, x1: e.clientX, y1: e.clientY }
    marqueeRef.current = next
    setMarquee(next)
  }
  function onPointerUp(e: React.PointerEvent) {
    const m = marqueeRef.current
    if (!m) return
    marqueeRef.current = null
    setMarquee(null)
    if (wrapRef.current?.hasPointerCapture(e.pointerId)) wrapRef.current.releasePointerCapture(e.pointerId)
    if (Math.abs(m.x1 - m.x0) < 4 && Math.abs(m.y1 - m.y0) < 4) return
    marqueeEndedAt.current = performance.now()
    const a = flowPoint({ clientX: Math.min(m.x0, m.x1), clientY: Math.min(m.y0, m.y1) })
    const b = flowPoint({ clientX: Math.max(m.x0, m.x1), clientY: Math.max(m.y0, m.y1) })
    const hit = nodesRef.current.filter((n) => {
      const h = n.measured?.height ?? DEFAULT_H
      return n.position.x < b.x && n.position.x + CARD_W > a.x && n.position.y < b.y && n.position.y + h > a.y
    }).map((n) => n.id)
    const ids = m.additive ? [...new Set([...m.base, ...hit])] : hit
    setSel(ids.length ? { kind: 'events', ids } : null)
  }

  // ---- keys
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => {})
  keyHandler.current = (e: KeyboardEvent) => {
    if (isTyping(e.target)) return
    const cmd = e.metaKey || e.ctrlKey
    const key = e.key.toLowerCase()
    if (cmd && key === 'z') { e.preventDefault(); if (e.shiftKey) client.redo(); else client.undo(); return }
    if (cmd && key === 'y') { e.preventDefault(); client.redo(); return }
    if (cmd && key === 'a') { e.preventDefault(); const ids = client.getState().map.events.map((x) => x.id); setSel(ids.length ? { kind: 'events', ids } : null); return }
    if (cmd || e.altKey) return
    const p = presentingRef.current
    if (p) {
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === ' ' || e.key === 'Enter') { e.preventDefault(); presentGo(p.index + 1) }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); presentGo(p.index - 1) }
      else if (e.key === 'Escape' || key === 'p') setPresenting(null)
      return
    }
    if (e.key === 'Escape') {
      if (recordingRef.current) setRecording(null)
      else if (armedRef.current) setArmed(null)
      else if (chainRef.current.length) { setChain([]); if (toolRef.current === 'chain') setTool('select') }
      else if (selRef.current) setSel(null)
      else if (toolRef.current !== 'select') setTool('select')
      else if (focusedWf) setFocusedWf(null)
      return
    }
    if (e.key === 'Enter') {
      if (recordingRef.current) { e.preventDefault(); finishRecording(); return }
      const ids = selIds()
      if (ids.length === 1) { e.preventDefault(); startEdit(ids[0], 'title') }
      return
    }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      const s = selRef.current
      if (!s) return
      e.preventDefault()
      if (s.kind === 'events') deleteEvents(s.ids)
      else if (client.apply([{ op: 'deleteLink', from: s.from, to: s.to }])) setSel(null)
      return
    }
    if (recordingRef.current) return
    const t = TOOL_KEYS[key]
    if (t === 'link') { setArmed((a) => (a ? null : { from: null })); return }
    if (t) { changeTool(toolRef.current === t && t !== 'select' ? 'select' : t); return }
    if (key === 't') tidy()
    else if (key === 'f') fit()
    else if (key === 'w') setWorkflowsOpen((o) => !o)
    else if (key === 'p') present(null)
    else if (key === 'g') client.setLayout({ view: client.getState().layout.view === 'flow' ? 'blueprint' : 'flow' })
  }
  useEffect(() => {
    const h = (e: KeyboardEvent) => keyHandler.current(e)
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  function changeTool(t: Tool) {
    setArmed(null)
    if (t !== 'chain') setChain([])
    setTool(t)
  }
  function tidy() {
    if (client.getState().layout.view !== 'flow') return
    client.setLayout({ placed: [] })
    setTidyRun((n) => n + 1)
  }
  function fit() { void rf.fitView({ padding: 0.2, maxZoom: 1, duration: MOVE_MS }) }

  // ---- the canvas context cards read
  const startEdit = (id: string, field: Field) => {
    if (editingRef.current && (editingRef.current.id !== id || editingRef.current.field !== field)) saveOpenField(false)
    if (!findEvent(client.getState().map, id) || presentingRef.current) return
    setArmed(null)
    selectOnly(id)
    if (['details', 'files', 'actor', 'system'].includes(field) && !client.getState().layout.expanded.includes(id)) {
      client.setLayout({ expanded: [...client.getState().layout.expanded, id] })
    }
    setEditing({ id, field })
  }
  const registerEditor = useCallback((commit: (() => void) | null, owner: object) => {
    if (commit) editor.current = { commit, owner }
    else if (editor.current?.owner === owner) editor.current = null
  }, [])

  const focused = focusedWf ? findWorkflow(map, focusedWf) : undefined
  const presentId = presenting ? presentOrder[presenting.index] : null
  const selectedSet = useMemo(() => {
    if (presentId) return new Set([presentId])
    if (recording) return new Set(recording.steps.slice(-1))
    return new Set(sel?.kind === 'events' ? sel.ids : [])
  }, [sel, presentId, recording])
  const chainSet = useMemo(() => new Set(recording ? recording.steps.slice(0, -1) : chain.slice(0, -1)), [chain, recording])
  const dimmed = useMemo(() => {
    const steps = presenting?.workflow ? presentOrder : !presenting && focused ? focused.steps : null
    if (!steps) return new Set<string>()
    const on = new Set(steps)
    return new Set(map.events.map((e) => e.id).filter((id) => !on.has(id)))
  }, [presenting, presentOrder, focused, map.events])

  const ctx: CanvasState = {
    selected: selectedSet,
    chain: chainSet,
    dimmed,
    hovered,
    refuse,
    pinned,
    expanded,
    editing,
    workspaceDir: workspace.dir,
    startEdit,
    commitEdit,
    cancelEdit,
    toggleExpanded: (id) => setExpanded([id], !client.getState().layout.expanded.includes(id)),
    togglePinned: (id) => setPinned([id], !client.getState().layout.pinned.includes(id)),
    setKind: (id, kind: Kind) => { client.apply([{ op: 'updateEvent', id, patch: { kind } }]) },
    registerEditor,
  }

  // ---- what React Flow draws
  const rfNodes = useMemo(() => nodes.map((n) => {
    const selected = selectedSet.has(n.id) && !presenting && !recording
    const draggable = view === 'flow' && tool !== 'pan' && !pinned.has(n.id) && !presenting
    // a pinned card ignores drags entirely: it neither moves nor pans the map (Pan tool aside)
    const className = pinned.has(n.id) && tool !== 'pan' ? 'nopan' : undefined
    return n.selected === selected && n.draggable === draggable && n.className === className ? n : { ...n, selected, draggable, className }
  }), [nodes, selectedSet, pinned, view, tool, presenting, recording])

  const edges = useMemo<LinkEdge[]>(() => {
    const path = recording ? recording.steps : chain
    const steps = new Set(path.slice(1).map((id, i) => `${path[i]}>${id}`))
    const flow = presenting?.workflow ? presentOrder : !presenting && focused ? focused.steps : null
    const onFlow = flow ? new Set(flow.slice(1).map((id, i) => `${flow[i]}>${id}`)) : null
    return map.links.map((l) => {
      const key = `${l.from}>${l.to}`
      const state: LinkState =
        sel?.kind === 'link' && sel.from === l.from && sel.to === l.to ? 'selected'
          : steps.has(key) ? 'chain'
            : onFlow ? (onFlow.has(key) ? 'lit' : 'dim')
              : hovered && (l.from === hovered || l.to === hovered) ? 'lit'
                : hovered ? 'dim' : null
      return { id: key, source: l.from, target: l.to, type: 'link', data: { label: l.label, state } }
    })
  }, [map.links, chain, recording, sel, hovered, focused, presenting, presentOrder])

  // the ghost line: where the next click will link from
  const linking = !!armed || chain.length > 0 || tool === 'chain'
  const head = armed ? (armed.from ?? (sel?.kind === 'events' && sel.ids.length === 1 ? sel.ids[0] : null))
    : chain.at(-1) ?? (tool === 'chain' && sel?.kind === 'events' ? sel.ids.at(-1) ?? null : null)
  const ghostFrom = linking && head ? nodes.find((n) => n.id === head) : undefined
  const ghost = ghostFrom && cursor
    ? { x1: ghostFrom.position.x + CARD_W, y1: ghostFrom.position.y + (ghostFrom.measured?.height ?? DEFAULT_H) / 2, x2: cursor.x, y2: cursor.y }
    : null

  useEffect(() => {
    const kind = KIND_LABEL[boxKind]
    const oneSelected = sel?.kind === 'events' && sel.ids.length === 1
    onHint(
      recording ? `Recording “${recording.title}”: click the steps in order. Enter when done, Esc to cancel.`
        : presenting ? null
          : armed ? (armed.from || oneSelected ? 'Linking: click the card to link to. Esc cancels.' : 'Linking: click where the link starts, then where it ends. Esc cancels.')
            : chain.length && tool !== 'chain' ? 'Chaining: each card you click links from the last. Let go of Ctrl to finish.'
              : tool === 'chain' ? 'Chain is on: click cards in order to link them, or open space to add one. Esc to stop.'
                : tool === 'box' ? `Box is on: click open space to add a ${kind}. Click Box again or press Esc to stop.`
                  : tool === 'insert' ? 'Insert: click a link to put a new step in the middle.'
                    : tool === 'area' ? 'Area: drag a box around the cards you want.'
                      : tool === 'pan' ? 'Pan: drag to move around.'
                        : null,
    )
  }, [armed, chain.length, sel, tool, boxKind, recording, presenting, onHint])

  const onNodesChange = useCallback((changes: NodeChange<EventNode>[]) => {
    const keep = changes.filter((c) => c.type !== 'select') // selection is Dora's, not React Flow's
    if (keep.length) setNodes((ns) => applyNodeChanges(keep, ns))
  }, [])

  const selEvents = sel?.kind === 'events' ? sel.ids.map((id) => findEvent(map, id)).filter((e): e is DoraEvent => !!e) : []
  const shared = <T,>(f: (e: DoraEvent) => T): T | null => (selEvents.every((e) => f(e) === f(selEvents[0])) ? f(selEvents[0]) : null)

  return (
    <div
      ref={wrapRef}
      className={`absolute inset-0 ${linking || tool === 'insert' || recording ? 'linking' : ''} ${tool === 'box' ? 'tool-box' : ''} ${tool === 'area' || shiftDown ? 'tool-area' : ''} ${tool === 'pan' ? 'tool-pan' : ''}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onDoubleClick={(e) => { if (onEmpty(e.target)) doubleClickPane(flowPoint(e)) }}
      data-testid="canvas"
      data-tool={tool}
      data-view={view}
      data-armed={armed ? '1' : undefined}
      data-chain={chain.join(' ') || undefined}
      data-selected={sel?.kind === 'events' ? sel.ids.join(' ') : sel ? `${sel.from}>${sel.to}` : undefined}
      data-recording={recording ? recording.steps.join(' ') || '-' : undefined}
      data-focused={focusedWf ?? undefined}
    >
      <CanvasContext.Provider value={ctx}>
        <ReactFlow<EventNode, LinkEdge>
          nodes={rfNodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onNodeClick={(e, n) => { if (!echo()) clickEvent(n.id, { mod: e.ctrlKey || e.metaKey, shift: e.shiftKey }) }}
          onNodeContextMenu={(e, n) => { if (e.ctrlKey) { fromContextMenu(e); clickEvent(n.id, { mod: true, shift: false }) } }}
          onPaneClick={(e) => { if (!echo()) clickPane(flowPoint(e), { mod: e.ctrlKey || e.metaKey }) }}
          onPaneContextMenu={(e) => { if (e.ctrlKey) { fromContextMenu(e); clickPane(flowPoint(e), { mod: true }) } }}
          onEdgeClick={(_, edge) => {
            saveOpenField(false)
            setArmed(null)
            if (toolRef.current === 'insert' && !recordingRef.current && !presentingRef.current) insertBetween(edge.source, edge.target)
            else if (!presentingRef.current) setSel({ kind: 'link', from: edge.source, to: edge.target })
          }}
          onNodeMouseEnter={(_, n) => setHovered(n.id)}
          onNodeMouseLeave={() => setHovered(null)}
          onNodeDragStart={() => { cancelAnimationFrame(anim.current); setDragging(true) }}
          onNodeDragStop={(_, _n, dragged) => {
            setDragging(false)
            const l = client.getState().layout
            const positions = { ...l.positions }
            for (const d of dragged) positions[d.id] = { x: snap(d.position.x), y: snap(d.position.y) }
            const ids = dragged.map((d) => d.id).filter((id) => !l.pinned.includes(id))
            client.setLayout({ placed: [...new Set([...l.placed, ...ids])], positions })
          }}
          onMoveEnd={(_, v) => store(viewportKey(workspace.dir), v)}
          defaultViewport={{ x: 96, y: 96, zoom: 1 }}
          minZoom={0.2}
          maxZoom={2}
          snapToGrid
          snapGrid={[GRID, GRID]}
          paneClickDistance={4}
          nodeDragThreshold={3}
          nodesConnectable={false}
          nodesDraggable={view === 'flow' && tool !== 'pan'}
          elementsSelectable={false}
          selectNodesOnDrag={false}
          selectionKeyCode={null}
          multiSelectionKeyCode={null}
          deleteKeyCode={null}
          panOnDrag={!(tool === 'area' || shiftDown)}
          disableKeyboardA11y
          zoomOnDoubleClick={false}
          panOnScroll
          proOptions={{ hideAttribution: true }}
          className="bg-canvas"
        >
          {/* the LD3 mark, very faint, behind everything; fixed to the screen so it never competes with the cards */}
          <div className="react-flow__background pointer-events-none absolute inset-0 flex items-center justify-center" aria-hidden data-testid="watermark">
            <img src="/ld3-watermark.webp" alt="" draggable={false} className="select-none" style={{ width: 'min(56vw, 880px)', opacity: 0.08 }} />
          </div>
          <Pattern />
          {view === 'blueprint' && lanes && <Lanes lanes={lanes.bands} width={lanes.width} />}
          {ghost && (
            <ViewportPortal>
              <svg className="pointer-events-none absolute left-0 top-0 overflow-visible" width={1} height={1} aria-hidden data-testid="ghost">
                <path
                  d={`M ${ghost.x1} ${ghost.y1} C ${ghost.x1 + 60} ${ghost.y1}, ${ghost.x2 - 60} ${ghost.y2}, ${ghost.x2} ${ghost.y2}`}
                  fill="none" stroke="var(--orange)" strokeWidth={1.5} strokeDasharray="5 4"
                />
              </svg>
            </ViewportPortal>
          )}
        </ReactFlow>
      </CanvasContext.Provider>

      {marquee && (
        <div
          className="pointer-events-none fixed z-30 rounded-1 border border-orange-line bg-orange-chain"
          style={{ left: Math.min(marquee.x0, marquee.x1), top: Math.min(marquee.y0, marquee.y1), width: Math.abs(marquee.x1 - marquee.x0), height: Math.abs(marquee.y1 - marquee.y0) }}
          data-testid="marquee"
        />
      )}

      <LeftRail
        tool={tool}
        armed={!!armed}
        boxKind={boxKind}
        workflowsOpen={workflowsOpen}
        presenting={!!presenting}
        canPresent={map.events.length > 0}
        onTool={changeTool}
        onLink={() => setArmed((a) => (a ? null : { from: null }))}
        onBoxKind={setBoxKind}
        onWorkflows={() => setWorkflowsOpen((o) => !o)}
        onPresent={() => (presenting ? setPresenting(null) : present(null))}
      />

      {selEvents.length > 0 && !presenting && !recording && !editing && (
        <div className="pointer-events-none absolute left-1/2 top-3 z-20 -translate-x-1/2">
          <SelectionBar
            count={selEvents.length}
            color={shared((e) => e.color)}
            kind={shared((e) => e.kind)}
            allPinned={selEvents.every((e) => pinned.has(e.id))}
            anyOpen={selEvents.some((e) => expanded.has(e.id))}
            onColor={(color) => updateMany(selEvents.map((e) => e.id), (e) => (e.color === color ? null : { color }))}
            onKind={(kind) => updateMany(selEvents.map((e) => e.id), (e) => (e.kind === kind ? null : { kind }))}
            onPin={(pin) => setPinned(selEvents.map((e) => e.id), pin)}
            onDetails={(open) => setExpanded(selEvents.map((e) => e.id), open)}
            onDelete={() => deleteEvents(selEvents.map((e) => e.id))}
            onClear={() => setSel(null)}
          />
        </div>
      )}

      {workflowsOpen && (
        <WorkflowsPanel
          workflows={map.workflows}
          focused={focusedWf}
          recording={recording}
          onFocus={(id) => { setFocusedWf(id); setPresenting(null) }}
          onPresent={(id) => present(id)}
          onRename={(id, title) => client.apply([{ op: 'updateWorkflow', id, patch: { title } }])}
          onDelete={(id) => { if (client.apply([{ op: 'deleteWorkflow', id }]) && focusedWf === id) setFocusedWf(null) }}
          onStartRecording={startRecording}
          onFinishRecording={finishRecording}
          onCancelRecording={() => setRecording(null)}
          onClose={() => setWorkflowsOpen(false)}
        />
      )}

      {presenting && (
        <Present
          title={presenting.workflow ? findWorkflow(map, presenting.workflow)?.title || 'Workflow' : 'The whole map'}
          steps={presentOrder.map((id) => findEvent(map, id)).filter((e): e is DoraEvent => !!e)}
          index={presenting.index}
          onGo={presentGo}
          onClose={() => setPresenting(null)}
        />
      )}

      {!presenting && <Toolbar view={view} onView={(v) => client.setLayout({ view: v })} onTidy={tidy} onFit={fit} onTour={onTour} />}
    </div>
  )
}
