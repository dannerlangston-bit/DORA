import { describe, expect, it } from 'vitest'
import { applyOps, describeOp, emptyMap, invertOps, normalizeMap, OpError, wouldLoop, type DoraEvent, type DoraMap, type Op } from '../shared/ops.js'

const ev = (id: string, title = id): DoraEvent => ({ id, kind: 'step', title, summary: '', details: '', files: [] })
const chain = (...ids: string[]): DoraMap => ({
  ...emptyMap('t'),
  events: ids.map((id) => ev(id)),
  links: ids.slice(1).map((id, i) => ({ from: ids[i], to: id, label: '' })),
})

describe('ops', () => {
  it('adds, links and deletes, and deleting an event drops its links', () => {
    let m = applyOps(emptyMap('t'), [{ op: 'addEvent', event: ev('a') }, { op: 'addEvent', event: ev('b') }, { op: 'addLink', link: { from: 'a', to: 'b', label: '' } }])
    expect(m.links).toHaveLength(1)
    m = applyOps(m, [{ op: 'deleteEvent', id: 'a' }])
    expect(m.events.map((e) => e.id)).toEqual(['b'])
    expect(m.links).toEqual([])
  })

  it('refuses loops, self links and duplicates, all or nothing', () => {
    const m = chain('a', 'b', 'c')
    expect(wouldLoop(m, 'c', 'a')).toBe(true)
    expect(wouldLoop(m, 'a', 'c')).toBe(false)
    expect(() => applyOps(m, [{ op: 'addLink', link: { from: 'c', to: 'a', label: '' } }])).toThrow(OpError)
    expect(() => applyOps(m, [{ op: 'addLink', link: { from: 'a', to: 'a', label: '' } }])).toThrow(/itself/)
    expect(() => applyOps(m, [{ op: 'addLink', link: { from: 'a', to: 'b', label: '' } }])).toThrow(/already links/)
    // the first op is fine, the second is refused: nothing applies
    expect(() => applyOps(m, [{ op: 'setTitle', title: 'x' }, { op: 'addLink', link: { from: 'c', to: 'a', label: '' } }])).toThrow()
    expect(m.title).toBe('t')
  })

  it('inverts every op back to the starting map', () => {
    const start = chain('a', 'b', 'c')
    const batches: Op[][] = [
      [{ op: 'addEvent', event: ev('d') }, { op: 'addLink', link: { from: 'c', to: 'd', label: '' } }],
      [{ op: 'deleteEvent', id: 'b' }],
      [{ op: 'updateEvent', id: 'a', patch: { title: 'Start', summary: 'begins' } }],
      [{ op: 'updateLink', from: 'a', to: 'b', label: 'then' }],
      [{ op: 'deleteLink', from: 'b', to: 'c' }],
      [{ op: 'setTitle', title: 'New' }],
    ]
    for (const ops of batches) {
      const after = applyOps(start, ops)
      const back = applyOps(after, invertOps(start, ops))
      expect(new Set(back.events.map((e) => JSON.stringify(e)))).toEqual(new Set(start.events.map((e) => JSON.stringify(e))))
      expect(new Set(back.links.map((l) => `${l.from}>${l.to}:${l.label}`))).toEqual(new Set(start.links.map((l) => `${l.from}>${l.to}:${l.label}`)))
      expect(back.title).toBe(start.title)
    }
    // a deleted event comes back in the same place in the list
    const back = applyOps(applyOps(start, [{ op: 'deleteEvent', id: 'b' }]), invertOps(start, [{ op: 'deleteEvent', id: 'b' }]))
    expect(back.events.map((e) => e.id)).toEqual(['a', 'b', 'c'])
  })

  it('reads a hand-written file leniently and keeps unknown fields', () => {
    const { map, problems } = normalizeMap({
      title: 'Mine',
      events: [{ id: 'a', title: 'A', owner: 'ai' }, { id: 'a' }, { title: 'no id' }, { id: 'b', kind: 'weird' }],
      links: [{ from: 'a', to: 'b' }, { from: 'a', to: 'zzz' }],
    }, 'fallback')
    expect(map.events.map((e) => e.id)).toEqual(['a', 'b'])
    expect((map.events[0] as unknown as { owner: string }).owner).toBe('ai')
    expect(map.events[1].kind).toBe('step')
    expect(map.links).toEqual([{ from: 'a', to: 'b', label: '' }])
    expect(problems).toHaveLength(2)
    expect(normalizeMap('nope', 'f').map.title).toBe('f')
  })

  it('describes edits in words for changes.log', () => {
    const m = chain('a', 'b')
    expect(describeOp(m, { op: 'updateEvent', id: 'a', patch: { title: 'Start' } })).toBe('renamed a from "a" to "Start"')
    expect(describeOp(m, { op: 'addLink', link: { from: 'a', to: 'b', label: '' } })).toBe('linked a -> b')
  })
})
