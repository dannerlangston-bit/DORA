import { describe, expect, it } from 'vitest'
import { CARD_W, COL_GAP, GROUP_GAP, placeEvents, ROW_GAP, type XY } from '../src/canvas/layout'

const link = (from: string, to: string) => ({ from, to, label: '' })
const noOverlap = (at: Record<string, XY>, h = 112) => {
  const ids = Object.keys(at)
  for (const a of ids) for (const b of ids) {
    if (a >= b) continue
    const A = at[a], B = at[b]
    const hit = A.x < B.x + CARD_W && B.x < A.x + CARD_W && A.y < B.y + h && B.y < A.y + h
    expect(hit, `${a} overlaps ${b}`).toBe(false)
  }
}

describe('placeEvents', () => {
  it('runs a story left to right, one column per step, 96px apart', async () => {
    const at = await placeEvents({ ids: ['a', 'b', 'c'], links: [link('a', 'b'), link('b', 'c')], heights: {}, positions: {}, pinned: new Set() })
    expect(at.b.x - at.a.x).toBe(CARD_W + COL_GAP)
    expect(at.c.x - at.b.x).toBe(CARD_W + COL_GAP)
    expect(at.a.y).toBe(at.b.y)
  })

  it('stacks branches in one column 32px apart and never overlaps', async () => {
    const at = await placeEvents({ ids: ['a', 'b', 'c', 'd'], links: [link('a', 'b'), link('a', 'c'), link('a', 'd')], heights: {}, positions: {}, pinned: new Set() })
    const ys = ['b', 'c', 'd'].map((id) => at[id].y).sort((p, q) => p - q)
    expect(ys[1] - ys[0]).toBe(112 + ROW_GAP)
    expect(at.b.x).toBe(at.c.x)
    noOverlap(at)
  })

  it('keeps a group where it was and leaves pinned cards alone', async () => {
    const first = await placeEvents({ ids: ['a', 'b'], links: [link('a', 'b')], heights: {}, positions: { a: { x: 400, y: 304 } }, pinned: new Set() })
    expect(first.a).toEqual({ x: 400, y: 304 })
    const pinned = await placeEvents({ ids: ['a', 'b', 'c'], links: [link('a', 'b')], heights: {}, positions: { ...first, c: { x: 13, y: 7 } }, pinned: new Set(['c']) })
    expect(pinned.c).toEqual({ x: 13, y: 7 })
    expect(pinned.a).toEqual(first.a)
  })

  it('puts a brand-new group under the map, and pushes groups apart instead of overlapping', async () => {
    const at = await placeEvents({ ids: ['a', 'b', 'x', 'y'], links: [link('a', 'b'), link('x', 'y')], heights: {}, positions: { a: { x: 0, y: 0 }, b: { x: 336, y: 0 } }, pinned: new Set() })
    expect(at.x.y).toBeGreaterThanOrEqual(112 + GROUP_GAP)
    noOverlap(at)
    // a tall card in the first group pushes the second group down
    const tall = await placeEvents({ ids: ['a', 'b', 'x', 'y'], links: [link('a', 'b'), link('x', 'y')], heights: { a: 400 }, positions: at, pinned: new Set() })
    expect(tall.x.y).toBeGreaterThanOrEqual(400 + GROUP_GAP)
  })
})
