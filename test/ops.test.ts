import { describe, expect, it } from 'vitest'
import { applyOps, describeOp, emptyMap, invertOps, makeEvent, normalizeMap, OpError, wouldLoop, type DoraEvent, type DoraMap, type Op } from '../shared/ops.js'
import { checkMap } from '../shared/check.js'
import { normalizeLayout } from '../server/dora-server.js'

const ev = (id: string, title = id): DoraEvent => makeEvent({ id, title })
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
      [{ op: 'addWorkflow', workflow: { id: 'w', title: 'Main', summary: '', steps: ['a', 'b'] } }],
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
    expect(map.events[1].kind).toBe('action')
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

describe('workflows', () => {
  const withFlow = (): DoraMap => ({ ...chain('a', 'b', 'c'), workflows: [{ id: 'w', title: 'Main', summary: '', steps: ['a', 'b', 'c'] }] })

  it('deleting an event takes it out of workflows, and undo puts it back in place', () => {
    const start = withFlow()
    const ops: Op[] = [{ op: 'deleteEvent', id: 'b' }]
    const after = applyOps(start, ops)
    expect(after.workflows[0].steps).toEqual(['a', 'c'])
    const back = applyOps(after, invertOps(start, ops))
    expect(back.workflows[0].steps).toEqual(['a', 'b', 'c'])
  })

  it('refuses steps that do not exist or repeat', () => {
    expect(() => applyOps(chain('a', 'b'), [{ op: 'addWorkflow', workflow: { id: 'w', title: '', summary: '', steps: ['a', 'zz'] } }])).toThrow(OpError)
    expect(() => applyOps(withFlow(), [{ op: 'updateWorkflow', id: 'w', patch: { steps: ['a', 'a'] } }])).toThrow(/twice/)
  })

  it('reads old kind names as the new ones, and drops missing workflow steps with a note', () => {
    const { map, problems } = normalizeMap({
      events: [{ id: 'a', kind: 'step' }, { id: 'b', kind: 'milestone' }, { id: 'c', kind: 'Security', color: 'blue' }, { id: 'd', color: 'orange' }],
      workflows: [{ id: 'w', title: 'W', steps: ['a', 'gone', 'b'] }],
    }, 'f')
    expect(map.events.map((e) => e.kind)).toEqual(['action', 'outcome', 'security', 'action'])
    expect(map.events.map((e) => e.color)).toEqual(['', '', 'blue', ''])
    expect(map.workflows[0].steps).toEqual(['a', 'b'])
    expect(problems.join(' ')).toMatch(/workflow step/)
  })
})

describe('dora check', () => {
  it('finds syntax errors with a line number', () => {
    const r = checkMap('{\n  "events": [\n    oops\n  ]\n}')
    expect(r.errors[0]).toMatch(/line 3/)
  })

  it('finds loops, missing summaries, unlinked events and broken workflows', () => {
    const r = checkMap(JSON.stringify({
      events: [
        { id: 'a', kind: 'start', title: 'A', summary: 'starts' },
        { id: 'b', title: 'B', summary: '' },
        { id: 'c', title: 'C', summary: 'c' },
        { id: 'lonely', title: 'L', summary: 'l', kind: 'stage' },
      ],
      links: [{ from: 'a', to: 'b' }, { from: 'b', to: 'c' }, { from: 'c', to: 'a' }],
      workflows: [{ id: 'w', title: 'W', steps: ['a', 'c'] }],
    }))
    expect(r.errors.join('\n')).toMatch(/loop: .*a -> b -> c -> a/)
    const w = r.warnings.join('\n')
    expect(w).toMatch(/No summary: b/)
    expect(w).toMatch(/Not linked to anything: lonely/)
    expect(w).toMatch(/unknown kind "stage"/)
    expect(w).toMatch(/no link a -> c/)
    expect(r.stats).toEqual({ events: 4, links: 3, workflows: 1 })
  })

  it('passes a clean map', () => {
    const r = checkMap(JSON.stringify({
      events: [{ id: 'a', kind: 'start', title: 'A', summary: 'a' }, { id: 'b', kind: 'outcome', title: 'B', summary: 'b' }],
      links: [{ from: 'a', to: 'b' }],
      workflows: [{ id: 'w', title: 'W', steps: ['a', 'b'] }],
    }))
    expect(r.errors).toEqual([])
    expect(r.warnings).toEqual([])
  })
})

describe('layout file', () => {
  it('reads a version 1 layout: its pins were cards placed by hand', () => {
    expect(normalizeLayout({ positions: { a: { x: 1, y: 2 } }, pinned: ['a'], expanded: ['a'] }))
      .toEqual({ version: 2, view: 'flow', positions: { a: { x: 1, y: 2 } }, placed: ['a'], pinned: [], expanded: ['a'] })
    expect(normalizeLayout({ version: 2, view: 'blueprint', placed: ['a'], pinned: ['b'] }).pinned).toEqual(['b'])
  })
})
