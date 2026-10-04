# Dora: design rules v1

Dora is a canvas for building a map that explains a project. It starts blank; you, your AI, or both
add events and link them, and you end up with a story of how the project works that someone can read
at a glance. It shares its map engine and look with LD3 Map. The difference: LD3 Map draws a project
from a ledger that already exists, while Dora is for creating the map.

These are constraints, not suggestions. A value not defined here comes from the closest token in
`src/index.css`; say so in the PR.

## 0. Principles

- **Install once, map anywhere.** Dora is a tool you install once and run inside whatever project
  you're explaining. The map lives in that project.
- **The person and the AI share one map.** Neither waits for the other, and neither overwrites the other.
- **The snapshot first, depth on request.** A card says what something does in a sentence; the
  dropdown says how and why.
- **It stays tidy without anyone arranging it, and never jumps around under you.**
- **A plain click never links.** Linking always involves Ctrl/Cmd or the Link button.
- LD3 Map's visual rules carry over: hierarchy from surface lift and hairlines, never shadows or
  gradients; weights 400 and 500 only; nothing under 12px except 11px chips; dark only.

## 1. Where it runs

- `dora` in a project folder (or `dora path/to/project`) creates `.dora/`, starts a server on
  `localhost:4317` (the next free port if it's taken), and prints the URL. Open it in VS Code's Simple
  Browser so the map sits in a tab beside the code.
- The first run also adds `.claude/skills/dora/SKILL.md`, which teaches the project's AI the file
  format and the rules in §9. It never overwrites one that's already there. `--no-skill` skips it.
- An empty folder works too: map an idea before the code exists.
- The server only answers pages on this machine: it binds to 127.0.0.1, checks the Host header, and
  accepts writes only as `application/json` (which other websites can't send without a CORS
  preflight, and Dora never grants one).
- Later: a VS Code extension that opens the map from the command palette in its own panel, with no
  terminal and no URL.

## 2. Files

```
.dora/
  map.json      content: events and links. The AI and the person both edit this.
  layout.json   card positions, pins, which cards are expanded. Only the canvas writes it.
  changes.log   one line per canvas edit, newest last, so the AI can see what the person changed.
  README.md     what this folder is, for anyone who finds it.
```

Content and layout are split so the AI never handles coordinates; when it adds an event, the canvas
places it. The format is in `skill/SKILL.md`. Reads are lenient: missing fields get defaults, unknown
fields are kept, and anything that can't be shown (a duplicate id, a link to a missing event) is left
off the canvas and reported in a banner, never silently dropped from view.

## 3. Sync

- **The AI's saves show up straight away.** The server watches `.dora/`; when `map.json` changes, open
  canvases update within about 100ms.
- **Your edits never wait on the network.** A canvas edit applies on screen immediately and is sent
  to the server as a small batch of ops (add, update or delete an event or link, rename the map),
  never as a whole file.
- **The AI's latest save always wins over the canvas's older copy.** The server applies each batch to
  the file as it is on disk at that moment.
- **Edits that make no sense are refused as a whole.** Shared rules in `shared/ops.js` refuse
  duplicate links, loops, self-links and links to missing events, all or nothing, with a sentence
  shown to the person.
- Writes are atomic (a temp file, then a rename).
- Every canvas edit adds a line to `changes.log`, e.g. `2026-10-04T14:02:11Z user linked cart -> pay`.
- **A broken file never blanks the map.** If `map.json` stops parsing (the AI is mid-edit or made a
  mistake), the canvas keeps showing the last good version, with a red banner naming the line.
- The dot in the header is neutral while in sync and `--warn` while reconnecting.

## 4. Event cards

Fixed width 240px, `--bg-surface`, 1px `--line-1` border, radius `--r-3`, padding 12px 14px.

**Snapshot (always visible)**
1. Kind chip: 11px uppercase (`START`, `STEP`, `DECISION`, `MILESTONE`, `NOTE`). It's a dropdown,
   so clicking it changes the kind.
2. Title: 14/500 `--text-1`. Wraps; never cut off with an ellipsis. "Untitled" in `--text-3` while empty.
3. Summary: 13px `--text-2`, up to 3 lines. One or two plain sentences on what it does. When empty,
   "Add a summary (double-click)" in `--text-3`, always shown so selecting a card never changes its
   height.
4. Footer: `n files` in 12px mono `--text-3`, and the `Details` toggle with a chevron.

**Details (the dropdown, collapsed by default)**
- `details` rendered as markdown: why it exists, how it works, what it hands off.
- Files as mono chips that link to `vscode://file/<project>/<path>`.
- Capped at 360px tall, then it scrolls inside the card. Opening it pushes nearby cards away (§7).
- Which cards are open is saved in `layout.json`.

**Editing:** double-click the title, summary, details or files to edit them in place. Title and
summary save on Enter; details and files save on Cmd/Ctrl+Enter. Clicking away also saves. Esc
cancels. Files are one path per line.

**States**
- Hovered: border `--line-2`, `--bg-hover`.
- Head (selected, and where the next link starts): 2px orange edge (border plus outline, so the card
  doesn't shift), orange fill at 20%.
- Chain (earlier events in the chain you're linking): orange border at 60%, fill at 8%.
- Refused (a link to it was just refused): border flashes `--err` for 600ms.
- Pinned: a 12px pin in the top-right corner, `--text-3`.

## 5. Canvas controls

| Action | Result |
|---|---|
| Click empty space, nothing selected | New event at that spot, pinned there, title ready to type |
| Click empty space, an event selected | New event linked from the selected one, auto-spaced into place, title ready to type |
| Type, then Enter | Names the new event |
| Click away from a new event without naming it | It's removed, and that click does nothing else |
| Click an event | Select it (head) |
| Click the selected event | Deselect it |
| Drag on empty space / scroll | Pan |
| Drag a card | Move it, snapped to 8px; it becomes pinned |
| Click a link | Select it |
| Delete / Backspace | Remove the selected event (with its links) or link |
| Enter | Rename the selected event |
| Esc | Cancel linking, else end the chain, else deselect |
| Cmd/Ctrl+Z, Shift+Cmd/Ctrl+Z (or Ctrl+Y) | Undo, redo. Covers everything made on this canvas |
| T | Tidy: re-space everything and unpin every card |
| F | Fit the whole map in view |
| L | Same as tapping Ctrl |

Keys never act while you're typing in a field.

## 6. Linking

**Hold Ctrl (or Cmd) to chain**
- While it's held, every event you click links from the one before it.
- If an event was selected when you pressed it, the chain starts from that event.
- The newest event is the head (dark orange). Earlier ones are light orange, as are the links between them.
- Ctrl-click empty space adds a new event at the end of the chain.
- An existing link isn't duplicated; the chain just carries on through it.
- **Letting go ends the chain.** The head stays selected; everything else goes back to normal.

**Tap Ctrl (or Cmd) for one link**
- A tap is pressing and releasing the key on its own, within 400ms, with no click, scroll or other key
  in between. So Cmd+Z and Ctrl+C are never taps.
- It arms one link. The next event you click is linked from the selected one, then you're back to
  normal (select and drag).
- If nothing is selected, the first click picks where the link starts and the second where it ends.
- Linking an already-linked pair selects that link.
- Esc, clicking empty space, or tapping again cancels. The Link button in the toolbar does the same as
  a tap, for anyone not on a keyboard.

**Every link**
- Points from the event clicked first to the event clicked second.
- Never a duplicate, never to itself, never a loop. A refused link flashes the target red and says
  why at the bottom of the screen.
- While a link is armed or a chain is active, a dashed orange line runs from the head to the cursor,
  and a hint at the bottom says what the next click will do.

**Mac and VS Code**
- On a Mac, Ctrl-click opens the right-click menu. Dora turns that into a Ctrl-click on the canvas,
  and Cmd does exactly the same.
- The page only gets key presses while it has focus. While it doesn't, a hint says "Click the map to
  use Ctrl and the shortcuts." Losing focus counts as letting go of Ctrl.

## 7. Spacing and auto-layout

Goal: the map always looks tidy without anyone arranging it, and it never jumps around under you.

**Engine:** ELK layered, left to right (`src/canvas/layout.ts`). Cards go in ordered top to bottom as
they currently sit, with model order respected, so adding one card only nudges its neighbours.

**Spacing (8px grid)**
| Between | Gap |
|---|---|
| Columns (story steps) | 96px: room for a link label |
| Cards in one column | 32px |
| Groups that don't connect | 80px |

Links leave the middle of a card's right edge and enter the middle of the next card's left edge, as
curves with a small arrowhead.

**What the layout does**
- Each group of linked events is laid out in story order.
- A group stays where it was. Its top-left corner holds still while its insides tidy up.
- A group containing a pinned card is anchored to that card.
- A pinned card with no links is left exactly where it is.
- If a group would run into another, the whole group moves down until there's an 80px gap. Groups
  never overlap.
- A brand-new group (for example, one the AI just wrote) goes below everything, aligned with the left
  edge of the map.

**When it runs**
- When events or links are added or removed, or pins change.
- When a card's height changes: details opened or closed, or text wrapping differently.
- Never while you're dragging. Never for height changes while you're typing; it catches up when you
  finish.
- Cards glide to their new spots over 300ms (ease-out); cards the AI just added appear straight in place.
- Positions are saved to `layout.json`, so the map opens exactly as you left it.

**Pins**
- Dragging a card pins it.
- An event placed by clicking empty space with nothing selected is pinned too, because you chose that spot.
- An event made from another (a linked click, or a Ctrl-click) is auto-spaced.
- Tidy (T) unpins everything and re-spaces.

## 8. Color

Orange means "in play." In LD3 Map, amber means status and is banned from selection. In Dora, orange
marks the selection and the chain, and nothing else.

| Token | Value | Use |
|---|---|---|
| `--orange` | `#f28c28` | head border and outline, ghost line, selected link |
| `--color-orange-head` | orange at 20% | head fill |
| `--orange-line` | orange at 60% | chain border, chain links |
| `--color-orange-chain` | orange at 8% | chain fill |

- Links: 1.5px `rgba(255,255,255,0.16)` with an arrowhead. Hovering an event lights its links to
  0.35 and dims the rest to 0.5. A selected link is 2px orange.
- Link labels are always shown, as an 11px mono chip on `--bg-raised` at the curve's midpoint.
- Canvas: `--bg-canvas` with a 16px dot grid at `rgba(255,255,255,0.04)`, as in LD3 Map.
- One floating toolbar, bottom-right: Link | Tidy | Fit | Controls.
- The header is top-left: "Dora", the map's title (double-click to rename), and the sync dot.

## 9. AI rules (shipped as `skill/SKILL.md`)

- Read `map.json` and the end of `changes.log` before changing anything; build on what the person
  changed and never put back what they removed.
- A summary is the snapshot: one or two plain sentences. Details are the depth. Point at files instead
  of pasting code.
- Never change an existing `id`. Never touch `layout.json`. Keep fields you don't recognise.
- To map a project: one `start`, then the main path, then the branches; 8–20 events.

## 10. Forbidden

No shadows, gradients or blur. No weights above 500. No pure #fff or #000. No orange on anything
except the head, the chain, the ghost line and a selected link. No default React Flow nodes, edges,
controls or background. No emoji. No ellipsis on titles. A plain click never makes a link.

## 11. Open questions

- Do `vscode://file/...` links open the file from inside VS Code's Simple Browser? They do from an
  ordinary browser. Needs checking on a Mac.
- One map per project, or several (`.dora/maps/*.json`)?
- Should link labels be editable on the canvas (select a link, press Enter)? Today only the AI sets them.
- Publish to npm (`npx dora-map`) once public, or keep installing from GitHub?
