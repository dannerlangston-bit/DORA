import { useCallback, useEffect, useRef, useState } from 'react'

/** A tap is Ctrl/Cmd pressed and released on its own, this fast, with nothing else in between. */
export const TAP_MS = 400

const isMod = (key: string) => key === 'Control' || key === 'Meta'

/** Focus is in a text field, so keys belong to the text, not the map. */
export function isTyping(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
}

/**
 * Ctrl (or Cmd) the way Dora reads it (DESIGN §6):
 * - held: chain mode while it stays down; `onRelease` when it comes up, which ends the chain.
 * - tapped on its own: `onTap`, which arms one link.
 * Any click, scroll or other key while it is down means it was a hold, so Cmd+Z is never a tap.
 * Leaving the window counts as letting go, since the key-up would never arrive.
 */
export function useModifier(handlers: { onTap: () => void; onRelease: () => void }) {
  const st = useRef({ held: false, at: 0, used: false })
  const cb = useRef(handlers)
  cb.current = handlers
  useEffect(() => {
    const reset = () => { st.current = { held: false, at: 0, used: false } }
    const down = (e: KeyboardEvent) => {
      if (isMod(e.key)) {
        if (!st.current.held) st.current = { held: true, at: performance.now(), used: false }
        return
      }
      if (st.current.held) st.current.used = true
    }
    const up = (e: KeyboardEvent) => {
      if (!isMod(e.key) || !st.current.held) return
      const { at, used } = st.current
      reset()
      if (!used && performance.now() - at < TAP_MS && !isTyping(e.target)) cb.current.onTap()
      else cb.current.onRelease()
    }
    const use = () => { if (st.current.held) st.current.used = true }
    const leave = () => { if (st.current.held) { reset(); cb.current.onRelease() } }
    window.addEventListener('keydown', down, true)
    window.addEventListener('keyup', up, true)
    window.addEventListener('pointerdown', use, true)
    window.addEventListener('wheel', use, { capture: true, passive: true })
    window.addEventListener('blur', leave)
    return () => {
      window.removeEventListener('keydown', down, true)
      window.removeEventListener('keyup', up, true)
      window.removeEventListener('pointerdown', use, true)
      window.removeEventListener('wheel', use, true)
      window.removeEventListener('blur', leave)
    }
  }, [])
  return st
}

/** State with a ref that is always current, for handlers that run between renders. */
export function useLive<T>(initial: T) {
  const [value, setValue] = useState(initial)
  const ref = useRef(value)
  const set = useCallback((next: T | ((prev: T) => T)) => {
    ref.current = typeof next === 'function' ? (next as (prev: T) => T)(ref.current) : next
    setValue(ref.current)
  }, [])
  return [value, set, ref] as const
}
