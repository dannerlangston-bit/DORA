import { useEffect, useRef, useState } from 'react'
import { CircleHelp, Link2, Maximize, Sparkles } from 'lucide-react'

const mac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
export const MOD = mac ? '⌘ or Ctrl' : 'Ctrl'

const btn = (active: boolean) =>
  `motion-fast flex h-7 items-center gap-1.5 rounded-2 px-2 text-sm transition-colors disabled:opacity-40 ${active ? 'bg-sel text-ink-1' : 'text-ink-2 hover:bg-sel hover:text-ink-1'}`

/**
 * One floating toolbar, bottom-right, as in LD3 Map: Link (one link, same as tapping Ctrl), Tidy,
 * Fit, and a card listing every control.
 */
export function Toolbar({ armed, canLink, onLink, onTidy, onFit }: { armed: boolean; canLink: boolean; onLink: () => void; onTidy: () => void; onFit: () => void }) {
  const [help, setHelp] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!help) return
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setHelp(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setHelp(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [help])
  return (
    <div ref={ref} className="absolute bottom-3 right-3 z-20 flex items-center gap-0.5 rounded-3 border border-line-1 bg-raised p-1" data-testid="toolbar">
      <button type="button" className={btn(armed)} onClick={onLink} disabled={!canLink} aria-pressed={armed} title={`Link two events (tap ${MOD}, or L)`} data-testid="link-button">
        <Link2 size={16} strokeWidth={1.5} /> Link
      </button>
      <span className="mx-0.5 h-5 w-px bg-line-1" aria-hidden />
      <button type="button" className={btn(false)} onClick={onTidy} title="Re-space the whole map and unpin every card (T)" data-testid="tidy-button">
        <Sparkles size={16} strokeWidth={1.5} /> Tidy
      </button>
      <button type="button" className={btn(false)} onClick={onFit} title="Fit the map in view (F)" data-testid="fit-button">
        <Maximize size={16} strokeWidth={1.5} /> Fit
      </button>
      <button type="button" className={btn(help)} onClick={() => setHelp((v) => !v)} aria-expanded={help} aria-label="Controls" title="Controls" data-testid="help-button">
        <CircleHelp size={16} strokeWidth={1.5} />
      </button>
      {help && (
        <div className="rise-in absolute bottom-full right-0 mb-3 w-[320px] rounded-3 border border-line-1 bg-raised p-3 text-xs text-ink-2" role="dialog" aria-label="Controls" data-testid="help">
          <Section title="Make">
            <Row k="Click empty space">a new event (linked from the selected one, if any)</Row>
            <Row k="Type, then Enter">names it; click away unnamed and it’s gone</Row>
            <Row k="Double-click text">edit the title, summary, details or files</Row>
            <Row k="Drag a card">move it; it stays pinned there</Row>
          </Section>
          <Section title="Link">
            <Row k={`Hold ${MOD}`}>every event you click links from the last; let go to finish</Row>
            <Row k={`Tap ${MOD}`}>link once: click the event to link to</Row>
            <Row k="Click a link">select it; Delete removes it</Row>
          </Section>
          <Section title="Keys">
            <Row k="Esc">cancel linking, then deselect</Row>
            <Row k="Delete">remove the selected event or link</Row>
            <Row k={`${mac ? '⌘' : 'Ctrl'}+Z`}>undo · add Shift to redo</Row>
            <Row k="T · F · L">tidy · fit · link</Row>
          </Section>
        </div>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-3 last:mb-0">
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-[0.04em] text-ink-3">{title}</div>
      <ul className="space-y-1">{children}</ul>
    </div>
  )
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="mono w-[116px] shrink-0 text-ink-1">{k}</span>
      <span>{children}</span>
    </li>
  )
}
