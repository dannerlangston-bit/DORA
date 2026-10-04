// @ts-check
/**
 * The vocabulary Dora explains a product in: what kinds of events there are, how they group, which
 * Blueprint lane each sits in, and the colors a person can give a card. Shared by the canvas, the
 * `dora check` command and the AI's instructions, so they all use the same words.
 */

/**
 * @typedef {'start' | 'touchpoint' | 'action' | 'data' | 'process' | 'storage' | 'security' | 'decision' | 'integration' | 'tracking' | 'outcome' | 'note'} Kind
 */

/** @type {{ group: string, kinds: { kind: Kind, label: string, hint: string }[] }[]} */
export const KIND_GROUPS = [
  {
    group: 'Experience',
    kinds: [
      { kind: 'start', label: 'Start', hint: 'What kicks off a workflow' },
      { kind: 'touchpoint', label: 'Touchpoint', hint: 'Where a person meets the product' },
      { kind: 'action', label: 'Action', hint: 'Something a person or the system does' },
    ],
  },
  {
    group: 'Data',
    kinds: [
      { kind: 'data', label: 'Data', hint: 'A kind of data coming in or moving' },
      { kind: 'process', label: 'Process', hint: 'Something data passes through that changes it' },
      { kind: 'storage', label: 'Storage', hint: 'Where data is kept' },
    ],
  },
  {
    group: 'Control',
    kinds: [
      { kind: 'security', label: 'Security', hint: 'A check things must pass: sign-in, permissions, encryption' },
      { kind: 'decision', label: 'Decision', hint: 'A fork: the path depends on a rule' },
      { kind: 'integration', label: 'Integration', hint: 'An outside system' },
    ],
  },
  {
    group: 'Visibility',
    kinds: [
      { kind: 'tracking', label: 'Tracking', hint: 'How things are recorded and watched' },
      { kind: 'outcome', label: 'Outcome', hint: 'What the customer ends up with' },
      { kind: 'note', label: 'Note', hint: 'Context, not a step' },
    ],
  },
]

/** @type {Kind[]} */
export const KINDS = KIND_GROUPS.flatMap((g) => g.kinds.map((k) => k.kind))

/** @type {Record<Kind, string>} */
export const KIND_LABEL = /** @type {Record<Kind, string>} */ (Object.fromEntries(KIND_GROUPS.flatMap((g) => g.kinds.map((k) => [k.kind, k.label]))))

/** Older names, read as their new kind so maps made before the vocabulary changed still open. */
/** @type {Record<string, Kind>} */
export const KIND_ALIASES = { step: 'action', milestone: 'outcome' }

/** @param {unknown} v @returns {Kind} */
export function toKind(v) {
  if (typeof v !== 'string') return 'action'
  const k = v.trim().toLowerCase()
  if (KINDS.includes(/** @type {Kind} */ (k))) return /** @type {Kind} */ (k)
  return KIND_ALIASES[k] ?? 'action'
}

/**
 * Blueprint lanes, top to bottom: the layers a product is explained in. Every kind lives in one.
 * @type {{ id: string, label: string, kinds: Kind[] }[]}
 */
export const LANES = [
  { id: 'customer', label: 'Customer', kinds: ['start', 'action', 'outcome'] },
  { id: 'touchpoints', label: 'Touchpoints', kinds: ['touchpoint'] },
  { id: 'data', label: 'Data', kinds: ['data'] },
  { id: 'processing', label: 'Processing', kinds: ['process', 'decision'] },
  { id: 'security', label: 'Security', kinds: ['security'] },
  { id: 'systems', label: 'Storage & systems', kinds: ['storage', 'integration'] },
  { id: 'tracking', label: 'Tracking', kinds: ['tracking'] },
  { id: 'notes', label: 'Notes', kinds: ['note'] },
]

/** @param {Kind} kind */
export const laneOf = (kind) => (LANES.find((l) => l.kinds.includes(kind)) ?? LANES[0]).id

/**
 * Card colors a person can give a card or a group of cards. Shown as the card's left stripe.
 * Orange is left out on purpose: on the canvas orange means "selected".
 * @type {{ id: string, label: string, value: string }[]}
 */
export const COLORS = [
  { id: 'blue', label: 'Blue', value: '#4c8dff' },
  { id: 'teal', label: 'Teal', value: '#2fb5a8' },
  { id: 'green', label: 'Green', value: '#5bb974' },
  { id: 'purple', label: 'Purple', value: '#a274e8' },
  { id: 'pink', label: 'Pink', value: '#e86fb0' },
  { id: 'red', label: 'Red', value: '#f0616d' },
  { id: 'yellow', label: 'Yellow', value: '#d9c64a' },
  { id: 'slate', label: 'Slate', value: '#8b95a7' },
]
export const COLOR_IDS = COLORS.map((c) => c.id)
