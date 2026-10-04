import { Activity, Cog, Database, Flag, GitFork, MonitorSmartphone, MousePointerClick, Play, Plug, ShieldCheck, StickyNote, Table2, type LucideIcon } from 'lucide-react'
import type { Kind } from '../../shared/kinds.js'

/** One icon per kind, shown on the card and in the pickers (lucide, 16px or smaller, --text-2). */
export const KIND_ICON: Record<Kind, LucideIcon> = {
  start: Play,
  touchpoint: MonitorSmartphone,
  action: MousePointerClick,
  data: Table2,
  process: Cog,
  storage: Database,
  security: ShieldCheck,
  decision: GitFork,
  integration: Plug,
  tracking: Activity,
  outcome: Flag,
  note: StickyNote,
}
