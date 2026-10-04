import { useCallback, useEffect, useState } from 'react'
import { client, useDora } from './lib/client'
import { STARTERS } from './lib/starters'
import { Canvas } from './canvas/Canvas'
import { Tour } from './Tour'

const TOUR_SEEN = 'dora:tour-seen'
const tourSeen = () => { try { return localStorage.getItem(TOUR_SEEN) === '1' } catch { return true } }

/** Dora: the header, the canvas, and the few messages the canvas needs to say out loud. */
export function App() {
  const s = useDora()
  const [hint, setHint] = useState<string | null>(null)
  const [focused, setFocused] = useState(true)
  // the tour opens by itself the first time; automated browsers skip it so tests start on the map
  const [touring, setTouring] = useState(() => !tourSeen() && !navigator.webdriver)
  const onHint = useCallback((h: string | null) => setHint(h), [])
  const onTour = useCallback(() => setTouring(true), [])
  const endTour = useCallback(() => { setTouring(false); try { localStorage.setItem(TOUR_SEEN, '1') } catch { /* private window */ } }, [])

  useEffect(() => {
    const sync = () => setFocused(document.hasFocus())
    sync()
    window.addEventListener('focus', sync)
    window.addEventListener('blur', sync)
    return () => { window.removeEventListener('focus', sync); window.removeEventListener('blur', sync) }
  }, [])

  useEffect(() => { if (s.map.title) document.title = `${s.map.title} · Dora` }, [s.map.title])

  if (!s.ready) return <div className="flex h-full items-center justify-center text-sm text-ink-3">Opening the map…</div>

  const message = s.notice ?? hint ?? (!focused ? 'Click the map to use Ctrl and the shortcuts' : null)
  return (
    <div className="relative h-full w-full">
      <Canvas onHint={onHint} onTour={onTour} />

      <header className="pointer-events-none absolute left-3 top-3 z-20 flex items-center gap-2">
        <div className="pointer-events-auto flex h-9 items-center gap-2 rounded-3 border border-line-1 bg-raised pl-1.5 pr-3">
          <MapTitle title={s.map.title} />
          <span
            className={`h-1.5 w-1.5 rounded-full ${s.connected ? 'bg-ink-3' : 'bg-warn'}`}
            title={s.connected ? `In sync with ${s.workspace.dir}/.dora/map.json` : 'Reconnecting to Dora…'}
            data-testid="sync-dot"
            data-connected={s.connected ? '1' : '0'}
          />
        </div>
      </header>

      <img src="/ld3-emblem.webp" alt="LD3" title="LD3" draggable={false} className="pointer-events-none absolute right-4 top-3 z-10 h-10 w-auto select-none opacity-90" data-testid="emblem" />

      {(s.fileError || s.problems.length > 0) && (
        <div className="absolute left-1/2 top-14 z-30 flex max-w-[560px] -translate-x-1/2 flex-col gap-1.5" data-testid="file-banner">
          {s.fileError && <Banner tone="err">{s.fileError}</Banner>}
          {s.problems.map((p) => <Banner key={p} tone="warn">In map.json: {p}.</Banner>)}
        </div>
      )}

      {s.map.events.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center" data-testid="empty">
          <div className="fade-in pointer-events-auto w-[min(560px,calc(100%-32px))] text-center">
            <div className="text-md font-medium text-ink-1">Start a map</div>
            <div className="mt-1 text-sm text-ink-3">Pick a starter to fill in, or press B and click anywhere to add your first box.</div>
            <div className="mt-4 grid grid-cols-2 gap-2 text-left">
              {STARTERS.map((st) => (
                <button
                  key={st.id}
                  type="button"
                  className="motion-fast rounded-3 border border-line-1 bg-surface p-3 transition-colors hover:border-line-3 hover:bg-hover"
                  onClick={() => client.apply(st.ops())}
                  data-testid={`starter-${st.id}`}
                >
                  <div className="text-sm font-medium text-ink-1">{st.title}</div>
                  <div className="mt-1 text-xs text-ink-3">{st.blurb}</div>
                </button>
              ))}
            </div>
            <div className="mt-4 text-xs text-ink-3">
              Or ask your AI to map this project. It writes <span className="mono text-ink-2">.dora/map.json</span> and the boxes show up here.
            </div>
          </div>
        </div>
      )}

      {message && (
        <div
          className={`pointer-events-none absolute bottom-3 left-1/2 z-20 max-w-[60%] -translate-x-1/2 rounded-2 border bg-raised px-2.5 py-1 text-xs ${s.notice ? 'border-line-2 text-ink-1' : 'border-line-1 text-ink-2'}`}
          aria-live="polite"
          data-testid={s.notice ? 'notice' : 'hint'}
        >
          {message}
        </div>
      )}

      {touring && <Tour onClose={endTour} />}
    </div>
  )
}

function Banner({ tone, children }: { tone: 'err' | 'warn'; children: React.ReactNode }) {
  return (
    <div className={`rise-in rounded-2 border bg-raised px-3 py-1.5 text-xs text-ink-1 ${tone === 'err' ? 'border-err' : 'border-warn'}`}>
      {children}
    </div>
  )
}

/** The project's name, top-left. Double-click to rename; the first box you name names it if you haven't. */
function MapTitle({ title }: { title: string }) {
  const [draft, setDraft] = useState<string | null>(null)
  if (draft === null) {
    return (
      <button
        type="button"
        className="motion-fast h-7 max-w-[360px] truncate rounded-2 px-1.5 text-base font-medium text-ink-1 transition-colors hover:bg-sel"
        onDoubleClick={() => setDraft(title)}
        title="Double-click to rename this project"
        data-testid="map-title"
      >
        {title || 'Untitled project'}
      </button>
    )
  }
  const save = () => {
    const t = draft.trim()
    setDraft(null)
    if (t && t !== title) client.apply([{ op: 'setTitle', title: t }])
  }
  return (
    <input
      autoFocus
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setDraft(null) }}
      onFocus={(e) => e.target.select()}
      className="h-7 w-[280px] rounded-2 bg-transparent px-1.5 text-base font-medium text-ink-1 outline outline-1 outline-line-3"
      aria-label="Project name"
    />
  )
}
