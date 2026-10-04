import { Hand, Link2, MousePointer2, Presentation, Route, SquareDashedMousePointer, SquarePlus, Waypoints, Workflow, type LucideIcon } from 'lucide-react'
import { KIND_GROUPS, type Kind } from '../../shared/kinds.js'
import { KIND_ICON } from './kindIcons'

/** What a click on the canvas does (DESIGN §5). One at a time; Esc goes back to Select. */
export type Tool = 'select' | 'area' | 'box' | 'chain' | 'insert' | 'pan'

const TOOLS: { id: Tool | 'link'; label: string; key: string; Icon: LucideIcon; tip: string }[] = [
  { id: 'select', label: 'Select', key: 'V', Icon: MousePointer2, tip: 'Click to select, Shift-click to add more, drag to move. Double-click empty space to add a box.' },
  { id: 'area', label: 'Area', key: 'A', Icon: SquareDashedMousePointer, tip: 'Drag a box around cards to select them all' },
  { id: 'box', label: 'Box', key: 'B', Icon: SquarePlus, tip: 'Stays on: every click in open space adds a box. Click again to turn off.' },
  { id: 'chain', label: 'Chain', key: 'C', Icon: Waypoints, tip: 'Stays on: each card you click links from the last, like holding Ctrl' },
  { id: 'link', label: 'Link', key: 'L', Icon: Link2, tip: 'One link: click the card to link to (same as tapping Ctrl)' },
  { id: 'insert', label: 'Insert', key: 'I', Icon: Route, tip: 'Click a link to put a new step in the middle' },
  { id: 'pan', label: 'Pan', key: 'H', Icon: Hand, tip: 'Drag to move around; clicks don’t change anything' },
]

export const TOOL_KEYS: Record<string, Tool | 'link'> = Object.fromEntries(TOOLS.map((t) => [t.key.toLowerCase(), t.id]))

const btn = (on: boolean) =>
  `motion-fast relative flex h-8 w-8 items-center justify-center rounded-2 transition-colors ${on ? 'bg-sel text-ink-1' : 'text-ink-2 hover:bg-sel hover:text-ink-1'}`

/**
 * The left toolbar: tools that change what a click does, then the workflow buttons. While Box is on,
 * a palette of kinds sits beside it so the next boxes can be any kind.
 */
export function LeftRail(props: {
  tool: Tool
  armed: boolean
  boxKind: Kind
  workflowsOpen: boolean
  presenting: boolean
  canPresent: boolean
  onTool: (t: Tool) => void
  onLink: () => void
  onBoxKind: (k: Kind) => void
  onWorkflows: () => void
  onPresent: () => void
}) {
  return (
    <div className="absolute left-3 top-[60px] z-20 flex items-start gap-2">
      <div className="flex flex-col items-center gap-0.5 rounded-3 border border-line-1 bg-raised p-1" role="toolbar" aria-orientation="vertical" aria-label="Tools" data-testid="rail">
        {TOOLS.map(({ id, label, key, Icon, tip }) => {
          const on = id === 'link' ? props.armed : props.tool === id
          return (
            <button
              key={id}
              type="button"
              className={btn(on)}
              aria-pressed={on}
              aria-label={`${label} (${key})`}
              title={`${label} (${key}): ${tip}`}
              data-tool={id}
              data-tour={`tool-${id}`}
              onClick={() => (id === 'link' ? props.onLink() : props.onTool(props.tool === id && id !== 'select' ? 'select' : (id as Tool)))}
            >
              <Icon size={16} strokeWidth={1.5} />
            </button>
          )
        })}
        <span className="my-1 h-px w-5 bg-line-1" aria-hidden />
        <button type="button" className={btn(props.workflowsOpen)} aria-pressed={props.workflowsOpen} aria-label="Workflows (W)" title="Workflows (W): the paths customers take through the map" data-testid="workflows-button" data-tour="workflows" onClick={props.onWorkflows}>
          <Workflow size={16} strokeWidth={1.5} />
        </button>
        <button type="button" className={`${btn(props.presenting)} disabled:opacity-40`} disabled={!props.canPresent} aria-pressed={props.presenting} aria-label="Present (P)" title="Present (P): walk through a workflow one step at a time" data-testid="present-button" data-tour="present" onClick={props.onPresent}>
          <Presentation size={16} strokeWidth={1.5} />
        </button>
      </div>

      {props.tool === 'box' && (
        <div className="rise-in w-[188px] rounded-3 border border-line-1 bg-raised p-1.5" role="radiogroup" aria-label="Kind of box" data-testid="kind-palette">
          <div className="px-1.5 pb-1 pt-0.5 text-xs text-ink-3">Click open space to add a</div>
          {KIND_GROUPS.map((g) => (
            <div key={g.group} className="mb-1 last:mb-0">
              <div className="px-1.5 pb-0.5 pt-1 text-[11px] font-medium uppercase tracking-[0.04em] text-ink-3">{g.group}</div>
              {g.kinds.map((k) => {
                const Icon = KIND_ICON[k.kind]
                const on = props.boxKind === k.kind
                return (
                  <button
                    key={k.kind}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    title={k.hint}
                    className={`motion-fast flex h-7 w-full items-center gap-2 rounded-2 px-1.5 text-left text-sm transition-colors ${on ? 'bg-sel text-ink-1' : 'text-ink-2 hover:bg-sel hover:text-ink-1'}`}
                    onClick={() => props.onBoxKind(k.kind)}
                    data-kind={k.kind}
                  >
                    <Icon size={14} strokeWidth={1.5} /> {k.label}
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
