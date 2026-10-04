import { ChevronDown, Pin, Trash2, X } from 'lucide-react'
import { COLORS, KIND_GROUPS, type Kind } from '../../shared/kinds.js'

const btn = 'motion-fast flex h-7 items-center gap-1.5 rounded-2 px-2 text-sm text-ink-2 transition-colors hover:bg-sel hover:text-ink-1'

/**
 * Shown while cards are selected: change all of them at once (DESIGN §5b). Color, kind, pin or
 * unpin, open or close details, delete.
 */
export function SelectionBar(props: {
  count: number
  color: string | null       // shared color id, '' for none, null when mixed
  kind: Kind | null          // shared kind, null when mixed
  allPinned: boolean
  anyOpen: boolean
  onColor: (id: string) => void
  onKind: (k: Kind) => void
  onPin: (pin: boolean) => void
  onDetails: (open: boolean) => void
  onDelete: () => void
  onClear: () => void
}) {
  return (
    <div className="rise-in pointer-events-auto flex items-center gap-1 rounded-3 border border-line-1 bg-raised p-1" role="toolbar" aria-label="Change selected cards" data-testid="selection-bar">
      <span className="mono px-2 text-xs text-ink-2" data-testid="selection-count">{props.count} selected</span>
      <span className="mx-0.5 h-5 w-px bg-line-1" aria-hidden />
      <div className="flex items-center gap-1 px-1" role="radiogroup" aria-label="Color">
        <Swatch on={props.color === ''} label="No color" onClick={() => props.onColor('')} />
        {COLORS.map((c) => <Swatch key={c.id} on={props.color === c.id} label={c.label} value={c.value} onClick={() => props.onColor(c.id)} id={c.id} />)}
      </div>
      <span className="mx-0.5 h-5 w-px bg-line-1" aria-hidden />
      <label className="relative flex h-7 items-center rounded-2 text-sm text-ink-2 hover:bg-sel hover:text-ink-1">
        <select
          className="h-7 cursor-pointer appearance-none bg-transparent pl-2 pr-6"
          value={props.kind ?? ''}
          onChange={(e) => props.onKind(e.target.value as Kind)}
          aria-label="Kind"
          data-testid="bulk-kind"
        >
          {props.kind === null && <option value="" disabled>Mixed kinds</option>}
          {KIND_GROUPS.map((g) => (
            <optgroup key={g.group} label={g.group} className="bg-raised">
              {g.kinds.map((k) => <option key={k.kind} value={k.kind} className="bg-raised text-ink-1">{k.label}</option>)}
            </optgroup>
          ))}
        </select>
        <ChevronDown size={14} strokeWidth={1.5} className="pointer-events-none absolute right-1.5" />
      </label>
      <button type="button" className={btn} onClick={() => props.onPin(!props.allPinned)} data-testid="bulk-pin">
        <Pin size={14} strokeWidth={1.5} fill={props.allPinned ? 'currentColor' : 'none'} /> {props.allPinned ? 'Unpin' : 'Pin'}
      </button>
      <button type="button" className={btn} onClick={() => props.onDetails(!props.anyOpen)} data-testid="bulk-details">
        {props.anyOpen ? 'Close details' : 'Open details'}
      </button>
      <button type="button" className={btn} onClick={props.onDelete} title="Delete (Delete key)" data-testid="bulk-delete">
        <Trash2 size={14} strokeWidth={1.5} /> Delete
      </button>
      <button type="button" className={`${btn} px-1.5`} onClick={props.onClear} aria-label="Deselect (Esc)" title="Deselect (Esc)">
        <X size={14} strokeWidth={1.5} />
      </button>
    </div>
  )
}

function Swatch({ on, label, value, onClick, id }: { on: boolean; label: string; value?: string; onClick: () => void; id?: string }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={on}
      aria-label={label}
      title={label}
      onClick={onClick}
      className={`motion-fast flex h-4 w-4 items-center justify-center overflow-hidden rounded-full border transition-[outline-color] ${on ? 'outline outline-2 outline-offset-1 outline-ink-2' : 'outline-transparent'} ${value ? 'border-transparent' : 'border-line-3'}`}
      style={value ? { background: value } : undefined}
      data-color={id ?? 'none'}
    >
      {!value && <svg width="16" height="16" aria-hidden><path d="M3 13 L13 3" stroke="var(--line-3)" strokeWidth="1.5" /></svg>}
    </button>
  )
}
