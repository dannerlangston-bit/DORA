// @ts-check
/**
 * Dora's map format, and the only ways it changes.
 *
 * The server applies ops to the file on disk; the canvas applies the same ops straight away so the
 * screen never waits on the network. One module, so both sides agree on what an edit means and on
 * which edits are refused (duplicate links, loops, unknown events).
 */

/** @typedef {'start' | 'step' | 'decision' | 'milestone' | 'note'} Kind */
/**
 * @typedef {object} DoraEvent
 * @property {string} id       short slug, never changes once created
 * @property {Kind} kind
 * @property {string} title
 * @property {string} summary  the snapshot: one or two plain sentences on what it does
 * @property {string} details  the dropdown: markdown, in depth
 * @property {string[]} files  project paths this event lives in
 */
/** @typedef {{ from: string, to: string, label: string }} DoraLink */
/** @typedef {{ version: 1, title: string, events: DoraEvent[], links: DoraLink[] }} DoraMap */
/**
 * @typedef {(
 *   | { op: 'addEvent', event: DoraEvent, index?: number }
 *   | { op: 'updateEvent', id: string, patch: Partial<Omit<DoraEvent, 'id'>> }
 *   | { op: 'deleteEvent', id: string }
 *   | { op: 'addLink', link: DoraLink }
 *   | { op: 'updateLink', from: string, to: string, label: string }
 *   | { op: 'deleteLink', from: string, to: string }
 *   | { op: 'setTitle', title: string }
 * )} Op
 */

/** @type {Kind[]} */
export const KINDS = ['start', 'step', 'decision', 'milestone', 'note']

/** An edit Dora refuses. The message is shown to the person as is. */
export class OpError extends Error {}

/** @param {string} title @returns {DoraMap} */
export function emptyMap(title) {
  return { version: 1, title, events: [], links: [] }
}

/** @param {unknown} v @returns {v is Record<string, unknown>} */
const isObject = (v) => typeof v === 'object' && v !== null && !Array.isArray(v)
/** @param {unknown} v */
const str = (v) => (typeof v === 'string' ? v : '')

/**
 * Lenient read of whatever is on disk. The AI writes this file by hand, so a missing field gets a
 * default instead of failing the whole map, and anything that can't be shown is dropped from view
 * and reported. Fields Dora doesn't know are kept, so saving never deletes what the AI added.
 * @param {unknown} raw
 * @param {string} fallbackTitle
 * @returns {{ map: DoraMap, problems: string[] }}
 */
export function normalizeMap(raw, fallbackTitle) {
  /** @type {string[]} */
  const problems = []
  if (!isObject(raw)) return { map: emptyMap(fallbackTitle), problems: ['map.json is not a JSON object'] }
  /** @type {DoraEvent[]} */
  const events = []
  const ids = new Set()
  let skipped = 0
  for (const e of Array.isArray(raw.events) ? raw.events : []) {
    const id = isObject(e) ? str(e.id).trim() : ''
    if (!isObject(e) || !id || ids.has(id)) { skipped++; continue }
    ids.add(id)
    const kind = /** @type {Kind} */ (KINDS.includes(/** @type {Kind} */ (e.kind)) ? e.kind : 'step')
    const files = Array.isArray(e.files) ? e.files.filter((f) => typeof f === 'string') : []
    events.push({ ...e, id, kind, title: str(e.title), summary: str(e.summary), details: str(e.details), files })
  }
  if (skipped) problems.push(`${skipped} event${skipped === 1 ? '' : 's'} without a unique id ${skipped === 1 ? 'is' : 'are'} not shown`)
  /** @type {DoraLink[]} */
  const links = []
  const pairs = new Set()
  let broken = 0
  for (const l of Array.isArray(raw.links) ? raw.links : []) {
    const from = isObject(l) ? str(l.from) : ''
    const to = isObject(l) ? str(l.to) : ''
    const key = `${from}\u0000${to}`
    if (!isObject(l) || !ids.has(from) || !ids.has(to) || from === to || pairs.has(key)) { broken++; continue }
    pairs.add(key)
    links.push({ ...l, from, to, label: str(l.label) })
  }
  if (broken) problems.push(`${broken} link${broken === 1 ? '' : 's'} point${broken === 1 ? 's' : ''} at missing events or repeat${broken === 1 ? 's' : ''} another link and ${broken === 1 ? 'is' : 'are'} not shown`)
  return { map: { ...raw, version: 1, title: str(raw.title) || fallbackTitle, events, links }, problems }
}

/** @param {DoraMap} map @param {string} id */
export const findEvent = (map, id) => map.events.find((e) => e.id === id)
/** @param {DoraMap} map @param {string} from @param {string} to */
export const findLink = (map, from, to) => map.links.find((l) => l.from === from && l.to === to)

/**
 * True when a link from → to would close a loop: `to` already leads back to `from`.
 * A map reads as a story, and stories run one way.
 * @param {DoraMap} map @param {string} from @param {string} to
 */
export function wouldLoop(map, from, to) {
  if (from === to) return true
  const seen = new Set([to])
  const queue = [to]
  while (queue.length) {
    const at = /** @type {string} */ (queue.shift())
    for (const l of map.links) {
      if (l.from !== at || seen.has(l.to)) continue
      if (l.to === from) return true
      seen.add(l.to)
      queue.push(l.to)
    }
  }
  return false
}

/** @param {DoraMap} map @param {string} id */
const label = (map, id) => {
  const t = findEvent(map, id)?.title
  return t ? `"${t}"` : id
}

/**
 * Apply one op. Returns a new map; never mutates. Throws OpError when the edit is refused.
 * @param {DoraMap} map @param {Op} op @returns {DoraMap}
 */
export function applyOp(map, op) {
  switch (op.op) {
    case 'addEvent': {
      if (!op.event?.id) throw new OpError('An event needs an id')
      if (findEvent(map, op.event.id)) throw new OpError(`There is already an event with the id ${op.event.id}`)
      const events = [...map.events]
      const at = op.index === undefined ? events.length : Math.max(0, Math.min(op.index, events.length))
      events.splice(at, 0, op.event)
      return { ...map, events }
    }
    case 'updateEvent': {
      if (!findEvent(map, op.id)) throw new OpError(`That event no longer exists (${op.id})`)
      const { id: _ignored, ...patch } = /** @type {Partial<DoraEvent>} */ (op.patch)
      return { ...map, events: map.events.map((e) => (e.id === op.id ? { ...e, ...patch } : e)) }
    }
    case 'deleteEvent': {
      if (!findEvent(map, op.id)) throw new OpError(`That event no longer exists (${op.id})`)
      return {
        ...map,
        events: map.events.filter((e) => e.id !== op.id),
        links: map.links.filter((l) => l.from !== op.id && l.to !== op.id),
      }
    }
    case 'addLink': {
      const { from, to } = op.link
      if (!findEvent(map, from) || !findEvent(map, to)) throw new OpError('Both ends of a link have to be events on the map')
      if (from === to) throw new OpError('An event can’t link to itself')
      if (findLink(map, from, to)) throw new OpError(`${label(map, from)} already links to ${label(map, to)}`)
      if (wouldLoop(map, from, to)) throw new OpError(`That link would make a loop: ${label(map, to)} already leads back to ${label(map, from)}`)
      return { ...map, links: [...map.links, { ...op.link, label: op.link.label ?? '' }] }
    }
    case 'updateLink': {
      if (!findLink(map, op.from, op.to)) throw new OpError('That link no longer exists')
      return { ...map, links: map.links.map((l) => (l.from === op.from && l.to === op.to ? { ...l, label: op.label } : l)) }
    }
    case 'deleteLink': {
      if (!findLink(map, op.from, op.to)) throw new OpError('That link no longer exists')
      return { ...map, links: map.links.filter((l) => !(l.from === op.from && l.to === op.to)) }
    }
    case 'setTitle':
      return { ...map, title: op.title }
    default:
      throw new OpError(`Unknown edit: ${/** @type {{ op: string }} */ (op).op}`)
  }
}

/**
 * Apply a batch. All or nothing: if one op is refused, none of them apply.
 * @param {DoraMap} map @param {Op[]} ops @returns {DoraMap}
 */
export function applyOps(map, ops) {
  return ops.reduce(applyOp, map)
}

/**
 * The ops that undo `ops`, given the map they were applied to.
 * @param {DoraMap} map @param {Op[]} ops @returns {Op[]}
 */
export function invertOps(map, ops) {
  /** @type {Op[][]} */
  const steps = []
  let at = map
  for (const op of ops) {
    steps.push(invertOp(at, op))
    at = applyOp(at, op)
  }
  return steps.reverse().flat()
}

/** @param {DoraMap} map @param {Op} op @returns {Op[]} */
function invertOp(map, op) {
  switch (op.op) {
    case 'addEvent':
      return [{ op: 'deleteEvent', id: op.event.id }]
    case 'updateEvent': {
      const before = /** @type {DoraEvent} */ (findEvent(map, op.id))
      /** @type {Record<string, unknown>} */
      const patch = {}
      for (const k of Object.keys(op.patch)) patch[k] = /** @type {Record<string, unknown>} */ (before)[k]
      return [{ op: 'updateEvent', id: op.id, patch }]
    }
    case 'deleteEvent': {
      const event = /** @type {DoraEvent} */ (findEvent(map, op.id))
      const index = map.events.indexOf(event)
      const links = map.links.filter((l) => l.from === op.id || l.to === op.id)
      return [{ op: 'addEvent', event, index }, ...links.map((link) => /** @type {Op} */ ({ op: 'addLink', link }))]
    }
    case 'addLink':
      return [{ op: 'deleteLink', from: op.link.from, to: op.link.to }]
    case 'updateLink':
      return [{ op: 'updateLink', from: op.from, to: op.to, label: findLink(map, op.from, op.to)?.label ?? '' }]
    case 'deleteLink':
      return [{ op: 'addLink', link: /** @type {DoraLink} */ (findLink(map, op.from, op.to)) }]
    case 'setTitle':
      return [{ op: 'setTitle', title: map.title }]
    default:
      return []
  }
}

/**
 * One line for changes.log, read by the AI to see what the person changed. Uses the map as it was
 * before the op, so a deleted event still has its title.
 * @param {DoraMap} map @param {Op} op @returns {string}
 */
export function describeOp(map, op) {
  switch (op.op) {
    case 'addEvent':
      return `added event ${op.event.id}${op.event.title ? ` "${op.event.title}"` : ''}`
    case 'updateEvent': {
      const before = findEvent(map, op.id)
      const fields = Object.keys(op.patch)
      if (fields.length === 1 && fields[0] === 'title') return `renamed ${op.id} from "${before?.title ?? ''}" to "${op.patch.title}"`
      return `edited ${fields.join(', ')} of ${op.id} ${label(map, op.id)}`
    }
    case 'deleteEvent':
      return `deleted event ${op.id} ${label(map, op.id)}`
    case 'addLink':
      return `linked ${op.link.from} -> ${op.link.to}`
    case 'updateLink':
      return `labelled link ${op.from} -> ${op.to} "${op.label}"`
    case 'deleteLink':
      return `unlinked ${op.from} -> ${op.to}`
    case 'setTitle':
      return `renamed the map to "${op.title}"`
    default:
      return 'unknown edit'
  }
}

/**
 * A fresh id for an event made on the canvas: short, readable, and unique on this map.
 * @param {DoraMap} map @returns {string}
 */
export function newEventId(map) {
  const taken = new Set(map.events.map((e) => e.id))
  for (;;) {
    const id = `e-${Math.random().toString(36).slice(2, 6)}`
    if (!taken.has(id)) return id
  }
}
