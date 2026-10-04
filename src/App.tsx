import { useCallback, useEffect, useState } from 'react'
import { client, useDora } from './lib/client'
import { Canvas } from './canvas/Canvas'

/** Dora: the header, the canvas, and the few messages the canvas needs to say out loud. */
export function App() {
  const s = useDora()
  const [hint, setHint] = useState<string | null>(null)
  const [focused, setFocused] = useState(true)
  const onHint = useCallback((h: string | null) => setHint(h), [])

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
      <Canvas onHint={onHint} />

      <header className="pointer-events-none absolute left-3 top-3 z-20 flex items-center gap-2">
        <div className="pointer-events-auto flex h-9 items-center gap-2.5 rounded-3 border border-line-1 bg-raised pl-3 pr-2.5">
          <span className="text-sm font-medium text-ink-1">Dora</span>
          <span className="h-4 w-px bg-line-1" aria-hidden />
          <MapTitle title={s.map.title} />
          <span
            className={`h-1.5 w-1.5 rounded-full ${s.connected ? 'bg-ink-3' : 'bg-warn'}`}
            title={s.connected ? `In sync with ${s.workspace.dir}/.dora/map.json` : 'Reconnecting to Dora…'}
            data-testid="sync-dot"
            data-connected={s.connected ? '1' : '0'}
          />
        </div>
      </header>

      {(s.fileError || s.problems.length > 0) && (
        <div className="absolute left-1/2 top-3 z-30 flex max-w-[560px] -translate-x-1/2 flex-col gap-1.5" data-testid="file-banner">
          {s.fileError && <Banner tone="err">{s.fileError}</Banner>}
          {s.problems.map((p) => <Banner key={p} tone="warn">In map.json: {p}.</Banner>)}
        </div>
      )}

      {s.map.events.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center" data-testid="empty">
          <div className="fade-in max-w-[360px] text-center">
            <div className="text-md font-medium text-ink-1">Click anywhere to add the first event</div>
            <div className="mt-1.5 text-sm text-ink-3">
              Or ask your AI to map this project. It writes <span className="mono text-ink-2">.dora/map.json</span> and the map fills in here as it goes.
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

/** The map's name, double-click to rename. */
function MapTitle({ title }: { title: string }) {
  const [draft, setDraft] = useState<string | null>(null)
  if (draft === null) {
    return (
      <span className="max-w-[320px] truncate text-sm text-ink-2" onDoubleClick={() => setDraft(title)} title="Double-click to rename the map" data-testid="map-title">
        {title || 'Untitled map'}
      </span>
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
      className="w-[240px] rounded-1 bg-transparent text-sm text-ink-1 outline outline-1 outline-offset-2 outline-line-3"
      aria-label="Map title"
    />
  )
}
