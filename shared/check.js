// @ts-check
/**
 * `dora check`: everything wrong with a map file, in words an AI (or a person) can act on.
 * Errors break the map; warnings make it harder to understand.
 */
import { COLOR_IDS, KIND_ALIASES, KINDS } from './kinds.js'
import { jsonErrorLine } from './jsonpos.js'
import { normalizeMap } from './ops.js'

/** @typedef {{ errors: string[], warnings: string[], stats: { events: number, links: number, workflows: number } }} CheckResult */

const MAX_TITLE = 60
const MAX_OUT = 4
const MAX_EVENTS = 40

/** @param {string[]} ids @param {number} [n] */
const some = (ids, n = 8) => ids.slice(0, n).join(', ') + (ids.length > n ? `, and ${ids.length - n} more` : '')

/**
 * @param {string} text  the contents of map.json
 * @returns {CheckResult}
 */
export function checkMap(text) {
  /** @type {string[]} */
  const errors = []
  /** @type {string[]} */
  const warnings = []
  let raw
  try {
    raw = JSON.parse(text)
  } catch (err) {
    const line = jsonErrorLine(text)
    errors.push(`map.json is not valid JSON${line ? ` (line ${line})` : ''}: ${err instanceof Error ? err.message : err}`)
    return { errors, warnings, stats: { events: 0, links: 0, workflows: 0 } }
  }
  const { map, problems } = normalizeMap(raw, 'map')
  errors.push(...problems)

  // things normalizeMap quietly fixed that the author should know about
  for (const e of Array.isArray(raw?.events) ? raw.events : []) {
    if (!e || typeof e !== 'object') continue
    if (typeof e.kind === 'string' && !KINDS.includes(e.kind) && !(e.kind in KIND_ALIASES)) warnings.push(`${e.id}: unknown kind "${e.kind}", read as "action". Use one of: ${KINDS.join(', ')}`)
    else if (typeof e.kind === 'string' && e.kind in KIND_ALIASES) warnings.push(`${e.id}: "${e.kind}" is an old kind name; use "${KIND_ALIASES[e.kind]}"`)
    if (e.color && !COLOR_IDS.includes(e.color)) warnings.push(`${e.id}: unknown color "${e.color}". Use one of: ${COLOR_IDS.join(', ')}, or leave it out`)
  }

  // loops: the story has to run one way
  const out = new Map(map.events.map((e) => [e.id, /** @type {string[]} */ ([])]))
  for (const l of map.links) out.get(l.from)?.push(l.to)
  const state = new Map()
  /** @type {string[]} */
  const stack = []
  /** @param {string} id @returns {string[] | null} */
  const visit = (id) => {
    state.set(id, 1)
    stack.push(id)
    for (const next of out.get(id) ?? []) {
      if (state.get(next) === 1) return [...stack.slice(stack.indexOf(next)), next]
      if (!state.has(next)) { const loop = visit(next); if (loop) return loop }
    }
    stack.pop()
    state.set(id, 2)
    return null
  }
  for (const e of map.events) {
    if (state.has(e.id)) continue
    const loop = visit(e.id)
    if (loop) { errors.push(`Links make a loop: ${loop.join(' -> ')}. Remove one of these links.`); break }
  }

  if (!map.events.length) warnings.push('The map is empty')
  else {
    if (!map.events.some((e) => e.kind === 'start')) warnings.push('No start event. Mark where each workflow begins with kind "start".')
    const noSummary = map.events.filter((e) => !e.summary.trim() && e.kind !== 'note').map((e) => e.id)
    if (noSummary.length) warnings.push(`No summary: ${some(noSummary)}. Every event needs one or two plain sentences on what it does.`)
    const untitled = map.events.filter((e) => !e.title.trim()).map((e) => e.id)
    if (untitled.length) warnings.push(`No title: ${some(untitled)}`)
    const long = map.events.filter((e) => e.title.length > MAX_TITLE).map((e) => e.id)
    if (long.length) warnings.push(`Titles over ${MAX_TITLE} characters: ${some(long)}. Keep titles to a few words; put the rest in the summary.`)
    if (map.events.length > 1) {
      const linked = new Set(map.links.flatMap((l) => [l.from, l.to]))
      const alone = map.events.filter((e) => !linked.has(e.id) && e.kind !== 'note').map((e) => e.id)
      if (alone.length) warnings.push(`Not linked to anything: ${some(alone)}`)
    }
    const busy = [...out].filter(([, to]) => to.length > MAX_OUT).map(([id]) => id)
    if (busy.length) warnings.push(`More than ${MAX_OUT} links out of: ${some(busy)}. Consider a decision event or splitting the step.`)
    if (map.events.length > MAX_EVENTS) warnings.push(`${map.events.length} events is a lot to read. Consider splitting the story into workflows, or keeping the top level coarse and putting depth in details.`)
  }

  for (const w of map.workflows) {
    if (!w.title.trim()) warnings.push(`Workflow ${w.id} has no title`)
    if (w.steps.length < 2) { warnings.push(`Workflow ${w.id} has ${w.steps.length} step${w.steps.length === 1 ? '' : 's'}; a workflow is a path of at least two`); continue }
    for (let i = 1; i < w.steps.length; i++) {
      const a = w.steps[i - 1], b = w.steps[i]
      if (!map.links.some((l) => l.from === a && l.to === b)) warnings.push(`Workflow ${w.id}: ${a} is followed by ${b}, but there is no link ${a} -> ${b}`)
    }
  }

  return { errors, warnings, stats: { events: map.events.length, links: map.links.length, workflows: map.workflows.length } }
}
