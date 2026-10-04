import { useEffect, useRef, useState } from 'react'
import { CircleHelp, Maximize, Sparkles } from 'lucide-react'

const mac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
export const MOD = mac ? '⌘ or Ctrl' : 'Ctrl'

const btn = (active: boolean) =>
  `motion-fast flex h-7 items-center gap-1.5 rounded-2 px-2 text-sm transition-colors disabled:opacity-40 ${active ? 'bg-sel text-ink-1' : 'text-ink-2 hover:bg-sel hover:text-ink-1'}`

/**
 * The view toolbar, bottom-right, as in LD3 Map: Flow or Blueprint, Tidy, Fit, and a card listing
 * every control (with the tour). Tools that change what a click does live in the left toolbar.
 */
export function Toolbar({ view, onView, onTidy, onFit, onTour }: { view: 'flow' | 'blueprint'; onView: (v: 'flow' | 'blueprint') => void; onTidy: () => void; onFit: () => void; onTour: () => void }) {
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
      <div className="flex items-center gap-0.5" role="radiogroup" aria-label="View" data-tour="view">
        <button type="button" role="radio" aria-checked={view === 'flow'} className={btn(view === 'flow')} onClick={() => onView('flow')} title="Flow: the story, left to right (G)" data-testid="view-flow">Flow</button>
        <button type="button" role="radio" aria-checked={view === 'blueprint'} className={btn(view === 'blueprint')} onClick={() => onView('blueprint')} title="Blueprint: the same map in lanes by layer (G)" data-testid="view-blueprint">Blueprint</button>
      </div>
      <span className="mx-0.5 h-5 w-px bg-line-1" aria-hidden />
      <button type="button" className={btn(false)} onClick={onTidy} disabled={view === 'blueprint'} title="Re-space the map. Pinned cards stay put (T)" data-testid="tidy-button" data-tour="tidy">
        <Sparkles size={16} strokeWidth={1.5} /> Tidy
      </button>
      <button type="button" className={btn(false)} onClick={onFit} title="Fit the map in view (F)" data-testid="fit-button">
        <Maximize size={16} strokeWidth={1.5} /> Fit
      </button>
      <button type="button" className={btn(help)} onClick={() => setHelp((v) => !v)} aria-expanded={help} aria-label="Help" title="Help" data-testid="help-button" data-tour="help">
        <CircleHelp size={16} strokeWidth={1.5} />
      </button>
      {help && (
        <div className="rise-in absolute bottom-full right-0 mb-3 w-[340px] rounded-3 border border-line-1 bg-raised p-3 text-xs text-ink-2" role="dialog" aria-label="Controls" data-testid="help">
          <button type="button" className="motion-fast mb-3 flex h-8 w-full items-center justify-center rounded-2 border border-line-2 bg-surface text-sm text-ink-1 transition-colors hover:border-line-3" onClick={() => { setHelp(false); onTour() }} data-testid="tour-button">
            Take the tour
          </button>
          <Section title="Make">
            <Row k="B, then click">add a box; Box stays on until you click it off</Row>
            <Row k="Double-click space">add a box from Select</Row>
            <Row k="Type, Enter">name it; click away unnamed and it’s gone</Row>
            <Row k="Double-click text">edit any words on a card</Row>
            <Row k="I, click a link">put a new step in the middle</Row>
          </Section>
          <Section title="Link">
            <Row k={`Hold ${MOD}`}>each card you click links from the last</Row>
            <Row k={`Tap ${MOD} or L`}>link once</Row>
            <Row k="C">Chain stays on: like holding Ctrl</Row>
          </Section>
          <Section title="Many cards">
            <Row k="A, drag">select everything in the box</Row>
            <Row k="Shift-click / drag">add to the selection</Row>
            <Row k="Selection bar">color, kind, pin, details, delete</Row>
            <Row k="Pin (card corner)">locks a card: no dragging, Tidy skips it</Row>
          </Section>
          <Section title="Keys">
            <Row k="V A B C L I H">select · area · box · chain · link · insert · pan</Row>
            <Row k="W · P · G">workflows · present · flow/blueprint</Row>
            <Row k="T · F">tidy · fit</Row>
            <Row k="Esc">cancel, then deselect, then back to Select</Row>
            <Row k={`${mac ? '⌘' : 'Ctrl'}+Z`}>undo · add Shift to redo</Row>
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
      <span className="mono w-[128px] shrink-0 text-ink-1">{k}</span>
      <span>{children}</span>
    </li>
  )
}
