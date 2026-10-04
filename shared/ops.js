// @ts-check
/**
 * Dora's map format, and the only ways it changes.
 *
 * The server applies ops to the file on disk; the canvas applies the same ops straight away so the
 * screen never waits on the network. One module, so both sides agree on what an edit means and on
 * which edits are refused (duplicate links, loops, unknown events).
 */
import { COLOR_IDS, KINDS, toKind } from './kinds.js'

export { KINDS }

/** @typedef {import('./kinds.js').Kind} Kind */
/**
 * @typedef {object} DoraEvent
 * @property {string} id       short slug, never changes once created
 * @property {Kind} kind
 * @property {string} title
 * @property {string} summary  the snapshot: one or two plain sentences on what it does
 * @property {string} details  the dropdown: markdown, in depth
 * @property {string[]} files  project paths this event lives in
 * @property {string} actor    who does it: "Customer", "Admin", "Nightly job"
 * @property {string} system   what it runs on: "Okta", "Postgres", "Stripe"
 * @property {string} color    a color id from kinds.js COLORS, or "" for none
 */
/** @typedef {{ from: string, to: string, label: string }} DoraLink */
/**
 * A named path through the map: one way a customer (or the system) goes through the product.
 * @typedef {{ id: string, title: string, summary: string, steps: string[] }} DoraWorkflow
 */
/** @typedef {{ version: 1, title: string, events: DoraEvent[], links: DoraLink[], workflows: DoraWorkflow[] }} DoraMap */
/**
 * @typedef {(
 *   | { op: 'addEvent', event: DoraEvent, index?: number }
 *   | { op: 'updateEvent', id: string, patch: Partial<Omit<DoraEvent, 'id'>> }
 *   | { op: 'deleteEvent', id: string }
 *   | { op: 'addLink', link: DoraLink }
 *   | { op: 'updateLink', from: string, to: string, label: string }
 *   | { op: 'deleteLink', from: string, to: string }
 *   | { op: 'addWorkflow', workflow: DoraWorkflow, index?: number }
 *   | { op: 'updateWorkflow', id: string, patch: Partial<Omit<DoraWorkflow, 'id'>> }
 *   | { op: 'deleteWorkflow', id: string }
 *   | { op: 'setTitle', title: string }
 * )} Op
 */

/** An edit Dora refuses. The message is shown to the person as is. */
export class OpError extends Error {}

/** @param {string} title @returns {DoraMap} */
export function emptyMap(title) {
  return { version: 1, title, events: [], links: [], workflows: [] }
}

/** @param {Partial<DoraEvent> & { id: string }} e @returns {DoraEvent} */
export function makeEvent(e) {
  return { kind: 'action', title: '', summary: '', details: '', files: [], actor: '', system: '', color: '', ...e }
}

/** @param {unknown} v @returns {v is Record<string, unknown>} */
const isObject = (v) => typeof v === 'object' && v !== null && !Array.isArray(v)
/** @param {unknown} v */
const str = (v) => (typeof v === 'string' ? v : '')
/** @param {number} n @param {string} one @param {string} many */
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

/**
 * Lenient read of whatever is on disk. The AI writes this file by hand, so a missing field gets a
 * default instead of failing the whole map, and anything that can't be shown is dropped from view
 * and reported. Fields Dora doesn't know are kept, so saving never deletes what the AI added.
 * Older kind names (step, milestone) are read as their new kinds.
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
    const files = Array.isArray(e.files) ? e.files.filter((f) => typeof f === 'string') : []
    const color = COLOR_IDS.includes(str(e.color)) ? str(e.color) : ''
    events.push({ ...e, id, kind: toKind(e.kind), title: str(e.title), summary: str(e.summary), details: str(e.details), files, actor: str(e.actor), system: str(e.system), color })
  }
  if (skipped) problems.push(`${plural(skipped, 'event', 'events')} without a unique id ${skipped === 1 ? 'is' : 'are'} not shown`)

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
  if (broken) problems.push(`${plural(broken, 'link points', 'links point')} at missing events or repeat${broken === 1 ? 's' : ''} another link and ${broken === 1 ? 'is' : 'are'} not shown`)

  /** @type {DoraWorkflow[]} */
  const workflows = []
  const wfIds = new Set()
  let badWf = 0, badSteps = 0
  for (const w of Array.isArray(raw.workflows) ? raw.workflows : []) {
    const id = isObject(w) ? str(w.id).trim() : ''
    if (!isObject(w) || !id || wfIds.has(id)) { badWf++; continue }
    wfIds.add(id)
    const all = Array.isArray(w.steps) ? w.steps.filter((s) => typeof s === 'string') : []
    const steps = all.filter((s, i) => ids.has(s) && all.indexOf(s) === i)
    badSteps += all.length - steps.length
    workflows.push({ ...w, id, title: str(w.title), summary: str(w.summary), steps })
  }
  if (badWf) problems.push(`${plural(badWf, 'workflow', 'workflows')} without a unique id ${badWf === 1 ? 'is' : 'are'} not shown`)
  if (badSteps) problems.push(`${plural(badSteps, 'workflow step points', 'workflow steps point')} at missing events or repeat${badSteps === 1 ? 's' : ''} and ${badSteps === 1 ? 'is' : 'are'} left out`)

  return { map: { ...raw, version: 1, title: str(raw.title) || fallbackTitle, events, links, workflows }, problems }
}

/** @param {DoraMap} map @param {string} id */
export const findEvent = (map, id) => map.events.find((e) => e.id === id)
/** @param {DoraMap} map @param {string} from @param {string} to */
export const findLink = (map, from, to) => map.links.find((l) => l.from === from && l.to === to)
/** @param {DoraMap} map @param {string} id */
export const findWorkflow = (map, id) => map.workflows.find((w) => w.id === id)

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
      events.splice(at, 0, makeEvent(op.event))
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
        workflows: map.workflows.map((w) => (w.steps.includes(op.id) ? { ...w, steps: w.steps.filter((s) => s !== op.id) } : w)),
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
    case 'addWorkflow': {
      if (!op.workflow?.id) throw new OpError('A workflow needs an id')
      if (findWorkflow(map, op.workflow.id)) throw new OpError(`There is already a workflow with the id ${op.workflow.id}`)
      checkSteps(map, op.workflow.steps)
      const workflows = [...map.workflows]
      const at = op.index === undefined ? workflows.length : Math.max(0, Math.min(op.index, workflows.length))
      workflows.splice(at, 0, { ...op.workflow, summary: op.workflow.summary ?? '' })
      return { ...map, workflows }
    }
    case 'updateWorkflow': {
      if (!findWorkflow(map, op.id)) throw new OpError('That workflow no longer exists')
      if (op.patch.steps) checkSteps(map, op.patch.steps)
      const { id: _ignored, ...patch } = /** @type {Partial<DoraWorkflow>} */ (op.patch)
      return { ...map, workflows: map.workflows.map((w) => (w.id === op.id ? { ...w, ...patch } : w)) }
    }
    case 'deleteWorkflow': {
      if (!findWorkflow(map, op.id)) throw new OpError('That workflow no longer exists')
      return { ...map, workflows: map.workflows.filter((w) => w.id !== op.id) }
    }
    case 'setTitle':
      return { ...map, title: op.title }
    default:
      throw new OpError(`Unknown edit: ${/** @type {{ op: string }} */ (op).op}`)
  }
}

/** @param {DoraMap} map @param {string[]} steps */
function checkSteps(map, steps) {
  if (!Array.isArray(steps)) throw new OpError('A workflow’s steps must be a list of event ids')
  for (const s of steps) if (!findEvent(map, s)) throw new OpError(`A workflow step points at an event that doesn’t exist (${s})`)
  if (new Set(steps).size !== steps.length) throw new OpError('A workflow can’t visit the same event twice')
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
      const workflows = map.workflows.filter((w) => w.steps.includes(op.id))
      return [
        { op: 'addEvent', event, index },
        ...links.map((link) => /** @type {Op} */ ({ op: 'addLink', link })),
        ...workflows.map((w) => /** @type {Op} */ ({ op: 'updateWorkflow', id: w.id, patch: { steps: w.steps } })),
      ]
    }
    case 'addLink':
      return [{ op: 'deleteLink', from: op.link.from, to: op.link.to }]
    case 'updateLink':
      return [{ op: 'updateLink', from: op.from, to: op.to, label: findLink(map, op.from, op.to)?.label ?? '' }]
    case 'deleteLink':
      return [{ op: 'addLink', link: /** @type {DoraLink} */ (findLink(map, op.from, op.to)) }]
    case 'addWorkflow':
      return [{ op: 'deleteWorkflow', id: op.workflow.id }]
    case 'updateWorkflow': {
      const before = /** @type {DoraWorkflow} */ (findWorkflow(map, op.id))
      /** @type {Record<string, unknown>} */
      const patch = {}
      for (const k of Object.keys(op.patch)) patch[k] = /** @type {Record<string, unknown>} */ (before)[k]
      return [{ op: 'updateWorkflow', id: op.id, patch }]
    }
    case 'deleteWorkflow': {
      const workflow = /** @type {DoraWorkflow} */ (findWorkflow(map, op.id))
      return [{ op: 'addWorkflow', workflow, index: map.workflows.indexOf(workflow) }]
    }
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
      return `added ${op.event.kind ?? 'action'} event ${op.event.id}${op.event.title ? ` "${op.event.title}"` : ''}`
    case 'updateEvent': {
      const before = findEvent(map, op.id)
      const fields = Object.keys(op.patch)
      if (fields.length === 1 && fields[0] === 'title') return `renamed ${op.id} from "${before?.title ?? ''}" to "${op.patch.title}"`
      if (fields.length === 1 && fields[0] === 'kind') return `changed ${op.id} ${label(map, op.id)} to a ${op.patch.kind} event`
      if (fields.length === 1 && fields[0] === 'color') return `colored ${op.id} ${label(map, op.id)} ${op.patch.color || 'none'}`
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
    case 'addWorkflow':
      return `added workflow ${op.workflow.id} "${op.workflow.title}" (${op.workflow.steps.join(' -> ')})`
    case 'updateWorkflow': {
      const fields = Object.keys(op.patch)
      if (fields.length === 1 && fields[0] === 'steps') return `changed the steps of workflow ${op.id} to ${op.patch.steps?.join(' -> ')}`
      return `edited ${fields.join(', ')} of workflow ${op.id}`
    }
    case 'deleteWorkflow':
      return `deleted workflow ${op.id} "${findWorkflow(map, op.id)?.title ?? ''}"`
    case 'setTitle':
      return `renamed the map to "${op.title}"`
    default:
      return 'unknown edit'
  }
}

/**
 * A fresh id for something made on the canvas: short, readable, and unique among `taken`.
 * @param {Iterable<string>} taken @param {string} [prefix] @returns {string}
 */
export function freshId(taken, prefix = 'e') {
  const used = new Set(taken)
  for (;;) {
    const id = `${prefix}-${Math.random().toString(36).slice(2, 6)}`
    if (!used.has(id)) return id
  }
}

/** @param {DoraMap} map */
export const newEventId = (map) => freshId(map.events.map((e) => e.id), 'e')
/** @param {DoraMap} map */
export const newWorkflowId = (map) => freshId(map.workflows.map((w) => w.id), 'w')
