import { LANES, laneOf, type Kind } from '../../shared/kinds.js'
import type { DoraLink } from '../../shared/ops.js'
import { CARD_W, COL_GAP, DEFAULT_H, placeEvents, ROW_GAP, type XY } from './layout'

/** Blueprint geometry (DESIGN §7b). */
export const LANE_HEADER = 192 // the lane names' column, left of the first card
export const LANE_PAD = 28     // above and below the cards in a lane
export const LANE_MIN = 120

export type LaneBand = { id: string; label: string; top: number; height: number; count: number }
export type Blueprint = { positions: Record<string, XY>; lanes: LaneBand[]; width: number }

/**
 * The same map, in lanes. Left to right is still the order things happen (the columns come from
 * the Flow layout's story steps); top to bottom is the layer each kind belongs to (Customer,
 * Touchpoints, Data, Processing, Security, Storage & systems, Tracking, Notes). Lanes with no cards
 * are left out. Blueprint is fully automatic: nothing is dragged or pinned here.
 */
export async function placeBlueprint(args: {
  ids: string[]
  kinds: Record<string, Kind>
  links: DoraLink[]
  heights: Record<string, number | undefined>
}): Promise<Blueprint> {
  const h = (id: string) => args.heights[id] ?? DEFAULT_H
  // story columns: lay the whole map out as one flow and read each card's column off its x
  const flow = await placeEvents({ ids: args.ids, links: args.links, heights: args.heights, positions: {}, pinned: new Set() })
  const xs = [...new Set(args.ids.map((id) => Math.round(flow[id].x)))].sort((a, b) => a - b)
  const column = (id: string) => xs.indexOf(Math.round(flow[id].x))

  const positions: Record<string, XY> = {}
  const lanes: LaneBand[] = []
  let top = 0
  for (const lane of LANES) {
    const here = args.ids.filter((id) => laneOf(args.kinds[id]) === lane.id)
    if (!here.length) continue
    const byColumn = new Map<number, string[]>()
    for (const id of here) byColumn.set(column(id), [...(byColumn.get(column(id)) ?? []), id])
    let tallest = 0
    for (const [col, ids] of byColumn) {
      ids.sort((a, b) => flow[a].y - flow[b].y)
      let y = top + LANE_PAD
      for (const id of ids) {
        positions[id] = { x: LANE_HEADER + col * (CARD_W + COL_GAP), y }
        y += h(id) + ROW_GAP
      }
      tallest = Math.max(tallest, y - ROW_GAP - (top + LANE_PAD))
    }
    const height = Math.max(LANE_MIN, tallest + LANE_PAD * 2)
    lanes.push({ id: lane.id, label: lane.label, top, height, count: here.length })
    top += height
  }
  return { positions, lanes, width: LANE_HEADER + Math.max(1, xs.length) * (CARD_W + COL_GAP) }
}
