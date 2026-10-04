import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { applyNodeChanges, ReactFlow, ReactFlowProvider, useReactFlow, ViewportPortal, type NodeChange, type Viewport } from '@xyflow/react'
import '@xyflow/react/dist/base.css'
import { findEvent, findLink, newEventId, type DoraEvent, type Kind, type Op } from '../../shared/ops.js'
import { client, useDora } from '../lib/client'
import { CanvasContext, EventNodeView, type CanvasState, type EventNode, type Field } from './EventNode'
import { isTyping, useLive, useModifier } from './keys'
import { CARD_W, DEFAULT_H, GRID, placeEvents, snap, type XY } from './layout'
import { LinkEdgeView, type LinkEdge, type LinkState } from './LinkEdge'
import { Pattern } from './Pattern'
import { Toolbar } from './Toolbar'

const nodeTypes = { event: EventNodeView }
const edgeTypes = { link: LinkEdgeView }
const MOVE_MS = 300

type Selection = { kind: 'event'; id: string } | { kind: 'link'; from: string; to: string } | null
type Armed = { from: string | null } | null
type CommitResult = 'saved' | 'discarded' | 'unchanged'

const ease = (t: number) => 1 - Math.pow(1 - t, 3)
const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const viewportKey = (dir: string) => `dora:viewport:${dir}`

export function Canvas({ onHint }: { onHint: (hint: string | null) => void }) {
  return (
    <ReactFlowProvider>
      <Board onHint={onHint} />
    </ReactFlowProvider>
  )
}

function Board({ onHint }: { onHint: (hint: string | null) => void }) {
  const { map, layout, workspace } = useDora()
  const rf = useReactFlow<EventNode, LinkEdge>()
  const [nodes, setNodes] = useState<EventNode[]>([])
  const nodesRef = useRef(nodes)
  nodesRef.current = nodes
  const [sel, setSel, selRef] = useLive<Selection>(null)
  const [chain, setChain, chainRef] = useLive<string[]>([])
  const [armed, setArmed, armedRef] = useLive<Armed>(null)
  const [editing, setEditing, editingRef] = useLive<{ id: string; field: Field } | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  const [refuse, setRefuse] = useState<string | null>(null)
  const [cursor, setCursor] = useState<XY | null>(null)
  const [dragging, setDragging] = useState(false)
  const [tidyRun, setTidyRun] = useState(0)

  /** events made on the canvas and not yet named; clicking away from one unnamed removes it */
  const fresh = useRef(new Set<string>())
  /** where an event made on the canvas first appears, before auto-spacing moves it */
  const spawnAt = useRef(new Map<string, XY>())
  const editor = useRef<{ commit: () => void; owner: object } | null>(null)
  const commitMod = useRef(false)
  const lastCommit = useRef<CommitResult | null>(null)
  /** when an unnamed new card was last removed: the click that removed it shouldn't make another */
  const discardedAt = useRef(0)
  const lastContextClick = useRef(0)
  const anim = useRef(0)
  const fitted = useRef(false)

  const pinned = useMemo(() => new Set(layout.pinned), [layout.pinned])
  const expanded = useMemo(() => new Set(layout.expanded), [layout.expanded])
  const selectedId = () => (selRef.current?.kind === 'event' ? selRef.current.id : null)

  const mod = useModifier({
    onTap: () => {
      if (armedRef.current) { setArmed(null); return }
      if (client.getState().map.events.length < 2) return
      setArmed({ from: null })
    },
    onRelease: () => setChain([]),
  })

  // ---- nodes follow the map: keep each card's position and measured size, swap in the new event
  useEffect(() => {
    setNodes((prev) => {
      const old = new Map(prev.map((n) => [n.id, n]))
      return map.events.map((e): EventNode => {
        const o = old.get(e.id)
        if (o) return o.data.event === e ? o : { ...o, data: { ...o.data, event: e } }
        const spawn = spawnAt.current.get(e.id)
        spawnAt.current.delete(e.id)
        const saved = client.getState().layout.positions[e.id]
        const position = spawn ?? saved
        // a starting size lets React Flow show the card at once instead of hiding it until measured,
        // so a new card's title box can take the first keystroke
        return { id: e.id, type: 'event', position: position ?? { x: 0, y: 0 }, initialWidth: CARD_W, initialHeight: DEFAULT_H, data: { event: e, unplaced: !position } }
      })
    })
  }, [map.events])

  // ---- auto-spacing (DESIGN §7). Re-runs when the story's shape, a card's height or the pins change.
  const structureKey = JSON.stringify([map.events.map((e) => e.id), map.links.map((l) => `${l.from}>${l.to}`), layout.pinned, tidyRun])
  const heightsNow = nodes.map((n) => Math.round((n.measured?.height ?? 0) / 4)).join(',')
  const heldHeights = useRef(heightsNow)
  if (!editing) heldHeights.current = heightsNow // nothing moves under someone typing
  const allMeasured = nodes.length === map.events.length && nodes.every((n) => n.measured?.height)

  useEffect(() => {
    if (dragging || !allMeasured) return
    let cancelled = false
    const current = nodesRef.current
    const positions: Record<string, XY> = {}
    const heights: Record<string, number> = {}
    for (const n of current) {
      if (!n.data.unplaced) positions[n.id] = n.position
      heights[n.id] = n.measured?.height ?? DEFAULT_H
    }
    const pins = new Set(client.getState().layout.pinned)
    void placeEvents({ ids: current.map((n) => n.id), links: client.getState().map.links, heights, positions, pinned: pins }).then((target) => {
      if (!cancelled) moveTo(target)
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structureKey, heldHeights.current, dragging, allMeasured])

  function moveTo(target: Record<string, XY>) {
    cancelAnimationFrame(anim.current)
    const from = new Map(nodesRef.current.map((n) => [n.id, n.position]))
    const moving = nodesRef.current.some((n) => { const t = target[n.id]; return t && !n.data.unplaced && (t.x !== n.position.x || t.y !== n.position.y) })
    const finish = () => {
      setNodes((ns) => ns.map((n) => (target[n.id] ? { ...n, position: target[n.id], data: n.data.unplaced ? { ...n.data, unplaced: false } : n.data } : n)))
      const saved = client.getState().layout.positions
      if (Object.keys(target).some((id) => saved[id]?.x !== target[id].x || saved[id]?.y !== target[id].y) || Object.keys(saved).some((id) => !target[id])) {
        client.setLayout({ positions: target })
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
    let saved: Viewport | null = null
    try { saved = JSON.parse(localStorage.getItem(viewportKey(workspace.dir)) ?? 'null') } catch { /* private window */ }
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

  function createEvent(at: XY, from: string | null): string | null {
    const m = client.getState().map
    const id = newEventId(m)
    const event: DoraEvent = { id, kind: m.events.length ? 'step' : 'start', title: '', summary: '', details: '', files: [] }
    const ops: Op[] = [{ op: 'addEvent', event }]
    if (from) ops.push({ op: 'addLink', link: { from, to: id, label: '' } })
    const spot = { x: snap(at.x - CARD_W / 2), y: snap(at.y - 24) }
    spawnAt.current.set(id, spot)
    if (!client.apply(ops)) { spawnAt.current.delete(id); return null }
    // a card placed on its own stays where it was put; a linked one is spaced automatically
    if (!from) {
      const l = client.getState().layout
      client.setLayout({ pinned: [...l.pinned, id], positions: { ...l.positions, [id]: spot } })
    }
    fresh.current.add(id)
    setSel({ kind: 'event', id })
    setEditing({ id, field: 'title' })
    return id
  }

  function forget(id: string) {
    fresh.current.delete(id)
    if (selRef.current?.kind === 'event' && selRef.current.id === id) setSel(null)
    setChain((c) => c.filter((x) => x !== id))
    const l = client.getState().layout
    if (l.pinned.includes(id) || l.expanded.includes(id)) client.setLayout({ pinned: l.pinned.filter((x) => x !== id), expanded: l.expanded.filter((x) => x !== id) })
  }

  /** remove an unnamed new event as though it was never made (no undo entry when possible) */
  function discard(id: string) {
    discardedAt.current = performance.now()
    const made = (ops: Op[]) => ops[0]?.op === 'addEvent' && ops[0].event.id === id
    if (!client.retract(made)) client.apply([{ op: 'deleteEvent', id }])
    forget(id)
  }

  function deleteEvent(id: string) {
    if (client.apply([{ op: 'deleteEvent', id }])) forget(id)
  }

  const commitEdit = (id: string, field: Field, raw: string): void => {
    setEditing(null)
    const e = findEvent(client.getState().map, id)
    const wasFresh = fresh.current.has(id)
    if (field === 'title') fresh.current.delete(id)
    let result: CommitResult = 'unchanged'
    if (e) {
      if (field === 'title' && !raw.trim() && wasFresh && !(mod.current.held || commitMod.current)) {
        discard(id)
        result = 'discarded'
      } else {
        const value = field === 'files' ? raw.split('\n').map((s) => s.trim()).filter(Boolean)
          : field === 'details' ? raw.replace(/\s+$/, '') : raw.replace(/\s+/g, ' ').trim()
        const before = e[field]
        if (JSON.stringify(before) !== JSON.stringify(value)) {
          client.apply([{ op: 'updateEvent', id, patch: { [field]: value } }])
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

  // ---- clicks (DESIGN §5, §6)
  function clickEvent(id: string, withMod: boolean) {
    if (editingRef.current && editingRef.current.id !== id) saveOpenField(withMod)
    if (!findEvent(client.getState().map, id)) return
    const a = armedRef.current
    if (a) {
      const from = a.from ?? selectedId()
      if (!from) { setArmed({ from: id }); setSel({ kind: 'event', id }); return }
      setArmed(null)
      if (from !== id && link(from, id) === 'exists') { setSel({ kind: 'link', from, to: id }); return }
      setSel({ kind: 'event', id })
      return
    }
    if (withMod) {
      const c = chainRef.current
      const head = c.at(-1) ?? selectedId()
      if (!head || head === id) { if (!c.length) setChain([id]); setSel({ kind: 'event', id }); return }
      if (link(head, id) === 'refused') return
      setChain([...(c.length ? c : [head]).filter((x) => x !== id), id])
      setSel({ kind: 'event', id })
      return
    }
    setSel(selectedId() === id ? null : { kind: 'event', id })
  }

  function clickPane(at: XY, withMod: boolean) {
    // pressing the mouse may already have saved (and removed) an open card before this click arrived
    if (saveOpenField(withMod) === 'discarded' || performance.now() - discardedAt.current < 500) return
    if (armedRef.current) { setArmed(null); return }
    if (withMod) {
      const c = chainRef.current
      const head = c.at(-1) ?? selectedId()
      const id = createEvent(at, head)
      if (id) setChain([...(c.length ? c : head ? [head] : []), id])
      return
    }
    if (selRef.current?.kind === 'link') { setSel(null); return }
    createEvent(at, selectedId())
  }

  const flowPoint = (e: { clientX: number; clientY: number }) => rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
  // On a Mac, Ctrl-click is the right-click menu. Dora takes it as a Ctrl-click instead; the click
  // event that may follow is ignored so it doesn't count twice.
  const fromContextMenu = (e: React.MouseEvent) => { e.preventDefault(); lastContextClick.current = performance.now() }
  const echo = () => performance.now() - lastContextClick.current < 400

  // ---- keys
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => {})
  keyHandler.current = (e: KeyboardEvent) => {
    if (isTyping(e.target)) return
    const cmd = e.metaKey || e.ctrlKey
    const key = e.key.toLowerCase()
    if (cmd && key === 'z') { e.preventDefault(); if (e.shiftKey) client.redo(); else client.undo(); return }
    if (cmd && key === 'y') { e.preventDefault(); client.redo(); return }
    if (cmd || e.altKey) return
    if (e.key === 'Escape') {
      if (armedRef.current) setArmed(null)
      else if (chainRef.current.length) setChain([])
      else setSel(null)
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      const s = selRef.current
      if (!s) return
      e.preventDefault()
      if (s.kind === 'event') deleteEvent(s.id)
      else if (client.apply([{ op: 'deleteLink', from: s.from, to: s.to }])) setSel(null)
    } else if (e.key === 'Enter') {
      const id = selectedId()
      if (id) { e.preventDefault(); startEdit(id, 'title') }
    } else if (key === 't') {
      tidy()
    } else if (key === 'f') {
      fit()
    } else if (key === 'l') {
      setArmed((a) => (a ? null : { from: null }))
    }
  }
  useEffect(() => {
    const h = (e: KeyboardEvent) => keyHandler.current(e)
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  function tidy() {
    client.setLayout({ pinned: [] })
    setTidyRun((n) => n + 1)
  }
  function fit() { void rf.fitView({ padding: 0.2, maxZoom: 1, duration: MOVE_MS }) }

  // ---- the canvas context cards read
  const startEdit = (id: string, field: Field) => {
    if (editingRef.current && (editingRef.current.id !== id || editingRef.current.field !== field)) saveOpenField(false)
    if (!findEvent(client.getState().map, id)) return
    setArmed(null)
    setSel({ kind: 'event', id })
    if ((field === 'details' || field === 'files') && !client.getState().layout.expanded.includes(id)) {
      client.setLayout({ expanded: [...client.getState().layout.expanded, id] })
    }
    setEditing({ id, field })
  }
  const registerEditor = useCallback((commit: (() => void) | null, owner: object) => {
    if (commit) editor.current = { commit, owner }
    else if (editor.current?.owner === owner) editor.current = null
  }, [])
  const head = armed ? (armed.from ?? (sel?.kind === 'event' ? sel.id : null)) : chain.length ? chain[chain.length - 1] : sel?.kind === 'event' ? sel.id : null
  const ctx: CanvasState = {
    head,
    chain: new Set(chain.slice(0, -1)),
    hovered,
    refuse,
    pinned,
    expanded,
    editing,
    workspaceDir: workspace.dir,
    startEdit,
    commitEdit,
    cancelEdit,
    toggleExpanded: (id) => {
      const l = client.getState().layout
      client.setLayout({ expanded: l.expanded.includes(id) ? l.expanded.filter((x) => x !== id) : [...l.expanded, id] })
    },
    setKind: (id, kind: Kind) => { client.apply([{ op: 'updateEvent', id, patch: { kind } }]) },
    registerEditor,
  }

  // ---- links
  const edges = useMemo<LinkEdge[]>(() => {
    const steps = new Set(chain.slice(1).map((id, i) => `${chain[i]}>${id}`))
    return map.links.map((l) => {
      const key = `${l.from}>${l.to}`
      const state: LinkState =
        sel?.kind === 'link' && sel.from === l.from && sel.to === l.to ? 'selected'
          : steps.has(key) ? 'chain'
            : hovered && (l.from === hovered || l.to === hovered) ? 'lit'
              : hovered ? 'dim' : null
      return { id: key, source: l.from, target: l.to, type: 'link', data: { label: l.label, state } }
    })
  }, [map.links, chain, sel, hovered])

  // ---- the ghost line: where the next click will link from
  const linking = !!armed || chain.length > 0
  const ghostFrom = linking && head ? nodes.find((n) => n.id === head) : undefined
  const ghost = ghostFrom && cursor
    ? { x1: ghostFrom.position.x + CARD_W, y1: ghostFrom.position.y + (ghostFrom.measured?.height ?? DEFAULT_H) / 2, x2: cursor.x, y2: cursor.y }
    : null

  useEffect(() => {
    onHint(
      armed ? (armed.from || sel?.kind === 'event' ? 'Linking: click the event to link to. Esc cancels.' : 'Linking: click where the link starts, then where it ends. Esc cancels.')
        : chain.length ? 'Chaining: each event you click links from the last. Click empty space to add one. Let go of Ctrl to finish.'
          : null,
    )
  }, [armed, chain.length, sel, onHint])

  const onNodesChange = useCallback((changes: NodeChange<EventNode>[]) => setNodes((ns) => applyNodeChanges(changes, ns)), [])

  return (
    <div
      className={`absolute inset-0 ${linking ? 'linking' : ''}`}
      onMouseMove={linking ? (e) => setCursor(flowPoint(e)) : undefined}
      data-testid="canvas"
      data-armed={armed ? '1' : undefined}
      data-chain={chain.join(' ') || undefined}
      data-selected={sel?.kind === 'event' ? sel.id : sel ? `${sel.from}>${sel.to}` : undefined}
    >
      <CanvasContext.Provider value={ctx}>
        <ReactFlow<EventNode, LinkEdge>
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onNodeClick={(e, n) => { if (!echo()) clickEvent(n.id, e.ctrlKey || e.metaKey) }}
          onNodeContextMenu={(e, n) => { if (e.ctrlKey) { fromContextMenu(e); clickEvent(n.id, true) } }}
          onPaneClick={(e) => { if (!echo()) clickPane(flowPoint(e), e.ctrlKey || e.metaKey) }}
          onPaneContextMenu={(e) => { if (e.ctrlKey) { fromContextMenu(e as React.MouseEvent); clickPane(flowPoint(e), true) } }}
          onEdgeClick={(_, edge) => { saveOpenField(false); setArmed(null); setSel({ kind: 'link', from: edge.source, to: edge.target }) }}
          onNodeMouseEnter={(_, n) => setHovered(n.id)}
          onNodeMouseLeave={() => setHovered(null)}
          onNodeDragStart={() => { cancelAnimationFrame(anim.current); setDragging(true) }}
          onNodeDragStop={(_, n) => {
            setDragging(false)
            const l = client.getState().layout
            const at = { x: snap(n.position.x), y: snap(n.position.y) }
            client.setLayout({ pinned: l.pinned.includes(n.id) ? l.pinned : [...l.pinned, n.id], positions: { ...l.positions, [n.id]: at } })
          }}
          onMoveEnd={(_, v) => { try { localStorage.setItem(viewportKey(workspace.dir), JSON.stringify(v)) } catch { /* private window */ } }}
          defaultViewport={{ x: 96, y: 96, zoom: 1 }}
          minZoom={0.2}
          maxZoom={2}
          snapToGrid
          snapGrid={[GRID, GRID]}
          paneClickDistance={4}
          nodeDragThreshold={3}
          nodesConnectable={false}
          elementsSelectable={false}
          selectionKeyCode={null}
          multiSelectionKeyCode={null}
          deleteKeyCode={null}
          disableKeyboardA11y
          zoomOnDoubleClick={false}
          panOnScroll
          proOptions={{ hideAttribution: true }}
          className="bg-canvas"
        >
          <Pattern />
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
      <Toolbar armed={!!armed} onLink={() => setArmed((a) => (a ? null : { from: null }))} onTidy={tidy} onFit={fit} canLink={map.events.length > 1} />
    </div>
  )
}
