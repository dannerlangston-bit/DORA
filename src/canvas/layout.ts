import type { ELK as Elk, ElkNode } from 'elkjs/lib/elk-api'
import type { DoraLink } from '../../shared/ops.js'

/** Spacing (DESIGN §7), all on the 8px grid. */
export const CARD_W = 240
export const COL_GAP = 96   // between story steps: room for a link label
export const ROW_GAP = 32   // between cards in one column
export const GROUP_GAP = 80 // between groups that don't connect
export const GRID = 8
export const DEFAULT_H = 112 // a collapsed card before it has been measured

export type XY = { x: number; y: number }
export type Rect = XY & { w: number; h: number }

/** ELK is most of the app's weight, so it loads after the page shows. */
let elk: Promise<Elk> | null = null
const loadElk = () => (elk ??= import('elkjs/lib/elk.bundled.js').then((m) => new m.default()))

export const snap = (v: number) => Math.round(v / GRID) * GRID

const ELK_OPTIONS = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.edgeRouting': 'POLYLINE',
  'elk.layered.spacing.nodeNodeBetweenLayers': String(COL_GAP),
  'elk.layered.spacing.edgeNodeBetweenLayers': '0',
  'elk.spacing.nodeNode': String(ROW_GAP),
  'elk.spacing.componentComponent': String(GROUP_GAP),
  'elk.separateConnectedComponents': 'true',
  // keep the order cards already have (we feed them top to bottom), so adding one card only
  // nudges its neighbours instead of reshuffling the map
  'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
  'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
  'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
}

/** Groups of events joined by links. */
export function components(ids: string[], links: DoraLink[]): string[][] {
  const parent = new Map(ids.map((id) => [id, id]))
  const find = (id: string): string => {
    let at = id
    while (parent.get(at) !== at) at = parent.get(at)!
    parent.set(id, at)
    return at
  }
  for (const l of links) {
    if (!parent.has(l.from) || !parent.has(l.to)) continue
    parent.set(find(l.from), find(l.to))
  }
  const groups = new Map<string, string[]>()
  for (const id of ids) {
    const r = find(id)
    groups.set(r, [...(groups.get(r) ?? []), id])
  }
  return [...groups.values()]
}

const overlaps = (a: Rect, b: Rect, gap: number) =>
  a.x < b.x + b.w + gap && b.x < a.x + a.w + gap && a.y < b.y + b.h + gap && b.y < a.y + a.h + gap

function bounds(ids: string[], at: Record<string, XY>, h: (id: string) => number): Rect {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const id of ids) {
    const p = at[id]
    x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y)
    x1 = Math.max(x1, p.x + CARD_W); y1 = Math.max(y1, p.y + h(id))
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
}

/**
 * Where every card should sit.
 *
 * - Each group of linked events is laid out left to right in story order (ELK layered).
 * - A group stays where it already was: its top-left corner holds still while its insides tidy up.
 * - Pinned cards (ones the person dragged) never move; a group with a pinned card is anchored to it.
 * - A group that would run into another moves down, whole, so groups never overlap.
 * - A brand-new group (say the AI just wrote one) goes below everything else.
 */
export async function placeEvents(args: {
  ids: string[]
  links: DoraLink[]
  heights: Record<string, number | undefined>
  positions: Record<string, XY | undefined>
  pinned: Set<string>
}): Promise<Record<string, XY>> {
  const { links, positions, pinned } = args
  const h = (id: string) => args.heights[id] ?? DEFAULT_H
  const linked = new Set(links.flatMap((l) => [l.from, l.to]))
  // a pinned card with no links is simply left where it is
  const loose = args.ids.filter((id) => pinned.has(id) && !linked.has(id) && positions[id])
  const looseSet = new Set(loose)
  const ids = args.ids.filter((id) => !looseSet.has(id))
  const idSet = new Set(ids)
  const live = links.filter((l) => idSet.has(l.from) && idSet.has(l.to))

  const order = ids.map((id, i) => ({ id, i, p: positions[id] }))
  order.sort((a, b) => (a.p?.y ?? Infinity) - (b.p?.y ?? Infinity) || (a.p?.x ?? Infinity) - (b.p?.x ?? Infinity) || a.i - b.i)

  const graph: ElkNode = {
    id: 'root',
    layoutOptions: ELK_OPTIONS,
    children: order.map(({ id }) => ({ id, width: CARD_W, height: h(id) })),
    edges: live.map((l, i) => ({ id: `l${i}`, sources: [l.from], targets: [l.to] })),
  }
  const out = await (await loadElk()).layout(graph)
  const raw: Record<string, XY> = {}
  for (const c of out.children ?? []) raw[c.id] = { x: c.x ?? 0, y: c.y ?? 0 }

  type Group = { ids: string[]; at: Record<string, XY>; fixed: boolean; known: boolean; prevY: number }
  const groups: Group[] = []
  for (const g of components(ids, live)) {
    const pins = g.filter((id) => pinned.has(id) && positions[id])
    const prev = g.filter((id) => positions[id])
    let dx = 0, dy = 0
    if (pins.length) {
      dx = pins.reduce((s, id) => s + positions[id]!.x - raw[id].x, 0) / pins.length
      dy = pins.reduce((s, id) => s + positions[id]!.y - raw[id].y, 0) / pins.length
    } else if (prev.length) {
      const was = bounds(prev, positions as Record<string, XY>, h)
      const now = bounds(prev, raw, h)
      dx = was.x - now.x
      dy = was.y - now.y
    }
    const at: Record<string, XY> = {}
    for (const id of g) at[id] = pinned.has(id) && positions[id] ? positions[id]! : { x: raw[id].x + dx, y: raw[id].y + dy }
    groups.push({ ids: g, at, fixed: pins.length > 0, known: prev.length > 0, prevY: prev.length ? bounds(prev, positions as Record<string, XY>, h).y : Infinity })
  }
  for (const id of loose) groups.push({ ids: [id], at: { [id]: positions[id]! }, fixed: true, known: true, prevY: positions[id]!.y })

  groups.sort((a, b) => Number(b.fixed) - Number(a.fixed) || Number(b.known) - Number(a.known) || a.prevY - b.prevY)
  const settled: Rect[] = []
  for (const g of groups) {
    let box = bounds(g.ids, g.at, h)
    if (!g.known && settled.length) {
      // new group: under everything, left-aligned with the map
      const left = Math.min(...settled.map((r) => r.x))
      const bottom = Math.max(...settled.map((r) => r.y + r.h))
      const ddx = left - box.x, ddy = bottom + GROUP_GAP - box.y
      for (const id of g.ids) g.at[id] = { x: g.at[id].x + ddx, y: g.at[id].y + ddy }
      box = bounds(g.ids, g.at, h)
    }
    if (!g.fixed) {
      for (let guard = 0; guard < 100; guard++) {
        const hit = settled.find((r) => overlaps(box, r, GROUP_GAP - 1))
        if (!hit) break
        const ddy = hit.y + hit.h + GROUP_GAP - box.y
        for (const id of g.ids) g.at[id] = { x: g.at[id].x, y: g.at[id].y + ddy }
        box = bounds(g.ids, g.at, h)
      }
    }
    settled.push(box)
  }

  const result: Record<string, XY> = {}
  for (const g of groups) for (const id of g.ids) result[id] = pinned.has(id) ? g.at[id] : { x: snap(g.at[id].x), y: snap(g.at[id].y) }
  return result
}
