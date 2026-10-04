# Dora: design rules v2

Dora is a canvas for building a map that explains a product. It starts blank. You, your AI, or both
add events and link them, and you end up with a picture of how the product works that someone can
read at a glance.

It's built for enterprise products, explained the way their teams explain them:
- who does what;
- what data comes in and what it passes through;
- how it's secured, where it's kept and how it's tracked;
- what the customer ends up with.

Dora shares its map engine and look with LD3 Map. The difference: LD3 Map draws a project from a
ledger that already exists, while Dora is for creating the map.

These are constraints, not suggestions. A value not defined here comes from the closest token in
`src/index.css`; say so in the PR.

## 0. Principles

- **Install once, map anywhere.** Dora is a tool you install once and run inside whatever project
  you're explaining. The map lives in that project.
- **The person and the AI share one map.** Neither waits for the other, and neither overwrites the other.
- **The snapshot first, depth on request.** A card says what something does in a sentence; the
  dropdown says how and why.
- **It stays tidy without anyone arranging it, and never jumps around under you.**
- **A click does what the active tool says, and nothing else.** A plain click in Select never creates
  or links. Tools that create or link say so in the hint and the cursor.
- **Orange means "in play":** what's selected, and the chain being linked. Card colors are the
  person's own labels and never use orange.
- LD3 Map's visual rules carry over: hierarchy from surface lift and hairlines, never shadows or
  gradients; weights 400 and 500 only; nothing under 12px except 11px chips; dark only.

## 1. Where it runs

- **Starting it.** `dora` in a project folder (or `dora path/to/project`) creates `.dora/`, starts a
  server on `localhost:4317` (the next free port if it's taken), and prints the URL. Open it in VS
  Code's Simple Browser so the map sits in a tab beside the code.
- **The AI's instructions.** The first run adds `.claude/skills/dora/SKILL.md`, which teaches the
  project's AI the vocabulary and the rules in §9.
  - It never overwrites that file unless asked.
  - When this Dora's instructions are newer (the file carries `dora-skill-version`), Dora says so and
    `dora --update-skill` replaces it.
  - `--no-skill` skips it.
- **Checking a map.** `dora check [folder]` lists what's wrong with the map (§9) and exits 1 when
  something must be fixed.
- **Ideas before code.** An empty folder works too: map an idea before the code exists.
- **Only this machine.** The server binds to 127.0.0.1, checks the Host header, and accepts writes
  only as `application/json`. Other websites can't send that without a CORS preflight, and Dora never
  grants one.
- **Later:** a VS Code extension that opens the map from the command palette in its own panel.

## 2. Files

```
.dora/
  map.json      content: events, links, workflows. The AI and the person both edit this.
  layout.json   card positions, placed and pinned cards, open cards, the view. Only the canvas writes it.
  changes.log   one line per canvas edit, newest last, so the AI can see what the person changed.
  README.md     what this folder is, for anyone who finds it.
```

**`map.json`**

- **Events:** `id`, `kind` (§3), `title`, `summary`, `details` (markdown), `actor` (who does it),
  `system` (what it runs on), `color` (§8) and `files`.
- **Links:** `{from, to, label}`.
- **Workflows:** `{id, title, summary, steps}`, where `steps` are event ids in order.
- **Reading is lenient.**
  - Missing fields get defaults, and unknown fields are kept.
  - The old kinds `step` and `milestone` read as `action` and `outcome`.
  - Anything that can't be shown (a duplicate id, a link or workflow step pointing at nothing) is left
    off the canvas and reported in a banner.

**`layout.json`** (version 2)
- `positions`: the Flow view's card positions.
- `placed`: cards a person put somewhere by hand. Auto-spacing leaves them; Tidy re-spaces them.
- `pinned`: cards locked in place (§5b). They can't be dragged, and Tidy leaves them too.
- `expanded`: cards with Details open.
- `view`: `flow` or `blueprint`.
- Version 1 files used `pinned` to mean placed, so they're read that way.

## 3. Kinds

Twelve kinds in four groups. Each has a 14px lucide icon, shown on the card and in every picker.

| Group | Kind | For | Blueprint lane |
|---|---|---|---|
| Experience | Start | what kicks off a workflow | Customer |
| | Touchpoint | where a person meets the product | Touchpoints |
| | Action | something a person or the system does | Customer |
| Data | Data | a kind of data coming in or moving | Data |
| | Process | something data passes through that changes it | Processing |
| | Storage | where data is kept | Storage & systems |
| Control | Security | a check things must pass | Security |
| | Decision | a fork by rule | Processing |
| | Integration | an outside system | Storage & systems |
| Visibility | Tracking | how things are recorded and watched | Tracking |
| | Outcome | what the customer ends up with | Customer |
| | Note | context, not a step | Notes |

Defined once, in `shared/kinds.js`, and used by the canvas, `dora check` and the AI's instructions.

## 4. Event cards

Fixed width 240px, `--bg-surface`, 1px `--line-1` border, radius `--r-3`, padding 12px 14px.

**Snapshot (always visible)**
1. Top row: the kind icon and the kind chip, which is a dropdown to change the kind. The **pin** sits
   in the top-right corner: faint while unpinned, solid `--text-1` while pinned.
2. Title: 14/500 `--text-1`. Wraps; never cut off with an ellipsis. "Untitled" in `--text-3` while empty.
3. Who · System: 12px mono `--text-3`, one line. Only shown when either is set.
4. Summary: 13px `--text-2`, up to 3 lines. "Add a summary (double-click)" when empty, always shown so
   selecting a card never changes its height.
5. Footer: `n files` in 12px mono `--text-3`, and the `Details` toggle.

**Details (the dropdown, collapsed by default)**
- The details, rendered as markdown.
- Then Who, System and Files (as mono chips that link to `vscode://file/<project>/<path>`).
- Capped at 360px tall, then it scrolls inside the card.

**Editing:** double-click any of those to edit it in place.
- Title, summary, who and system save on Enter.
- Details and files save on Cmd/Ctrl+Enter.
- Clicking away saves; Esc cancels.

**States**
| State | Look |
|---|---|
| Hovered | border `--line-2`, `--bg-hover` |
| Selected | 2px orange edge (border plus outline, so the card doesn't shift), orange fill at 20% |
| Earlier in a chain, or a recorded workflow | orange border at 60%, fill at 8% |
| Off the workflow being shown | opacity 0.35 |
| Refused | border flashes `--err` for 600ms |
| Colored | a 3px stripe down the left edge (§8) |

## 5. Tools

A vertical toolbar on the left, below the header, styled as in LD3 Map: 32px buttons, 16px icons, the
active one on `--sel-bg`. One tool is active at a time. Each has a key, and Esc works down through:
cancel what's happening, then deselect, then back to Select.

| Tool | Key | A click on open space | A click on a card | Notes |
|---|---|---|---|---|
| Select | V | deselects | selects it; Shift-click adds to the selection | double-click open space adds a box; drag moves cards; Shift-drag on open space selects an area |
| Area | A | drag a box: selects every card it touches | selects it | Shift adds to the selection |
| Box | B | adds a box of the chosen kind, linked from the selected card if exactly one is selected | selects it | **stays on** until clicked again, Esc or another tool. A palette of kinds sits beside the toolbar while it's on; the last kind is remembered |
| Chain | C | adds a box at the end of the chain | links from the last card in the chain | **stays on**; the same as holding Ctrl |
| Link | L | cancels | links from the selected card, then back to the previous tool | the same as tapping Ctrl |
| Insert | I | nothing | selects it | click a **link** to put a new step in the middle: from → new → to, and the same in every workflow that had from → to |
| Pan | H | dragging pans, even over cards | nothing | for reading or presenting without changing anything |

Below a divider come **Workflows** (W) and **Present** (P).

**Creating a box:** the new box opens with its title ready to type. Click away from it unnamed and
it's removed, and that click does nothing else (unless you're chaining).

**Keys:**
- Delete removes what's selected.
- Enter renames a single selected card.
- Cmd/Ctrl+A selects every card.
- Cmd/Ctrl+Z undoes; add Shift (or use Ctrl+Y) to redo.
- T tidies, F fits, G switches Flow and Blueprint.
- Keys never act while you're typing.

## 5b. Many cards at once

While cards are selected, a **selection bar** floats at the top center. It shows the count and lets
you change all of them at once:
- **Color:** none, or one of eight.
- **Kind:** "Mixed kinds" when they differ.
- **Pin / Unpin:** "Unpin" when every selected card is pinned.
- **Open / close details.**
- **Delete.**
- **Deselect.**

Each change is one undo step. Dragging any selected card moves all the selected cards that aren't pinned.

**Pin = locked.** A pinned card:
- can't be dragged, and dragging on it doesn't pan the map either;
- isn't moved by auto-spacing or Tidy.

Unpinning leaves the card where it is, as a placed card.

## 6. Linking

**Hold Ctrl (or Cmd) to chain.**
- While it's held, every card you click links from the one before it. If a card was selected, the
  chain starts there.
- The newest card is selected (dark orange); earlier ones are light orange, as are the links between them.
- Ctrl-click on open space adds a box at the end of the chain.
- An existing link isn't duplicated; the chain carries on through it.
- Letting go ends the chain. The Chain tool (C) does the same until Esc.

**Tap Ctrl (or Cmd) for one link.**
- A tap is the key on its own: released within 400ms, with no click, scroll or other key in between.
  So Cmd+Z is never a tap.
- The next card you click links from the selected one, then things go back to normal.
- If nothing is selected, the first click picks where the link starts and the second where it ends.
- Linking an already-linked pair selects that link.

**Every link**
- Points from first to second.
- Never a duplicate, never to itself, never a loop. A refused link flashes the target red and says why.
- While linking, a dashed orange line runs from the card it will start at to the cursor.

**Mac:** Ctrl-click opens the right-click menu, so Dora turns it into a Ctrl-click on the canvas. The
page only gets keys while it has focus; a hint says so when it doesn't, and losing focus counts as
letting go of Ctrl.

## 6b. Workflows

A workflow is a named path through the map: one way a customer (or the system) goes through the
product. Shared events, like sign-in or the warehouse, appear once, and several workflows run
through them.

- **The panel.** The Workflows panel (W) sits top-right, 300px wide. It lists each workflow with its
  step count, a play button and delete (on hover). Double-click a name to rename it.
- **Lighting a path.** Click a workflow to light its path: the cards on it stay, the rest fade to 0.35,
  its links light to 0.35 and the others dim. Click it again or press Esc to stop.
- **Making one.** Click **New workflow**, name it, then click its steps in order.
  - The steps show as a chain.
  - A missing link between two steps is added; a refused one, such as a loop, isn't.
  - Click the last step again to take it back.
  - Enter or Done saves it; Esc cancels.
- **Deleting.** Deleting an event takes it out of every workflow, and undo puts it back in place.

## 6c. Present

Present (P, or a workflow's play button) walks a workflow one step at a time.
- **What it shows.** Without a workflow chosen, it shows the focused one, then the first one, then the
  whole map in reading order (following links, left to right).
- **On the map.** The current card is selected and centered above the panel; cards off the workflow fade.
- **The panel.** A bottom panel, up to 520px wide, shows the step's kind, title, who and system,
  summary and details. Opening cards would re-space the map mid-presentation, so the panel shows them
  instead.
- **Moving.** Arrow keys, Space or Enter go forward and back. Clicking a card on the workflow jumps to
  it. Esc or P closes.

## 7. Spacing and auto-layout (Flow)

Goal: the map always looks tidy without anyone arranging it, and it never jumps around under you.

**Engine:** ELK layered, left to right (`src/canvas/layout.ts`). Cards go in ordered top to bottom as
they currently sit, so adding one card only nudges its neighbours.

| Between | Gap |
|---|---|
| Columns (story steps) | 96px: room for a link label |
| Cards in one column | 32px |
| Groups that don't connect | 80px |

**What the layout does**
- Each group of linked events is laid out in story order.
- A group stays where it was: its top-left corner holds still while its insides tidy up.
- A group containing a placed or pinned card is anchored to that card.
- A placed or pinned card with no links is left exactly where it is.
- If a group would run into another, the whole group moves down until there's an 80px gap. Groups
  never overlap.
- A brand-new group (one the AI just wrote, say) goes below everything, aligned with the left edge of
  the map.

**When it runs**
- When events or links are added or removed, when pins or placements change, or when a card's height
  changes.
- Never while dragging, and never for height changes while typing.
- Cards glide to their new spots over 300ms; cards the AI just added appear straight in place.

**Placed cards**
- A card you drag stays where you put it.
- So does a box made on open space with nothing to link from.
- A box made from another is auto-spaced.
- Tidy (T) re-spaces every card that isn't pinned.

## 7b. Blueprint

The same map in horizontal lanes, the "service blueprint" format enterprise teams use. Toggle it at
the bottom right, or press G; the view is saved in `layout.json`.

- **Two directions.** Left to right is still the story: the columns come from the Flow layout. Top to
  bottom is the layer: Customer, Touchpoints, Data, Processing, Security, Storage & systems, Tracking,
  Notes.
- **Lanes.** Lanes with no cards are left out. Each lane is at least 120px tall, with 28px above and
  below its cards; cards in one column of a lane stack 32px apart.
- **Bands.** They alternate transparent and `rgba(255,255,255,0.015)`, with a `--line-1` rule between
  them.
- **Lane names.** Each lane's name and card count sit in a small `--bg-raised` chip pinned 68px from
  the screen's left edge, so it stays readable however far you scroll.
- **Fully automatic.** Nothing is dragged in Blueprint, and Tidy is off. Flow positions are untouched,
  so switching back returns every card to where it was.

## 8. Color

| Token | Value | Use |
|---|---|---|
| `--orange` | `#f28c28` | selected border and outline, ghost line, selected link, tour ring |
| `--color-orange-head` | orange at 20% | selected fill |
| `--orange-line` | orange at 60% | chain border, chain links, area-select border |
| `--color-orange-chain` | orange at 8% | chain fill, area-select fill |

**Card colors** are the person's own labels: a 3px stripe down the card's left edge, nothing else.
The eight choices are blue `#4c8dff`, teal `#2fb5a8`, green `#5bb974`, purple `#a274e8`, pink
`#e86fb0`, red `#f0616d`, yellow `#d9c64a` and slate `#8b95a7`. There's no orange, because orange
means selected. A color should mean one thing across a map (an owner, a phase); a Note can say what.

**Links:** 1.5px `rgba(255,255,255,0.16)` with an arrowhead. Hovering a card lights its links to
0.35 and dims the rest to 0.5; a selected link is 2px orange. Labels always show, as an 11px mono
chip at the curve's midpoint.

**Canvas:** `--bg-canvas` with a 16px dot grid at `rgba(255,255,255,0.04)`.

## 9. AI rules (shipped as `skill/SKILL.md`)

- **Read first.** Read `map.json` and the end of `changes.log` before changing anything. Build on what
  the person changed; never put back what they removed.
- **One card per thing.** An event is something that happens, or a thing data passes through, that a
  person would name. It's never a file or a class; files go in `files`.
- **Workflows come first.** Walk each one through start → touchpoint → data → processing → security →
  storage → tracking → outcome. Reuse shared events.
- **Hard rules.** Never change an existing `id`. Never touch `layout.json`. Keep fields you don't
  recognise.
- **Check your work.** Run `dora check` after editing.
  - **Must fix:** broken JSON (with the line), loops, links or workflow steps pointing at nothing.
  - **Worth fixing:** no start, missing summaries or titles, unlinked events, titles over 60
    characters, more than 4 links out of one card, more than 40 events, unknown kinds or colors,
    workflow steps that aren't linked.

## 10. Starters and the empty map

An empty map shows "Start a map" with two starters:
- **Customer journey:** 8 events and a workflow, from sign-up to a ready workspace.
- **Data pipeline:** 9 events and a workflow. Data splits by type, passes checks, and becomes a report.

Both are written in plain words, as examples to overwrite. The empty map also says how to add a box
by hand (B, then click) and that the AI can fill the map.

## 11. Tour and guide

- **The tour** opens by itself the first time (automated browsers skip it) and from Help → Take the
  tour.
  - It has 15 steps, each with a short title and body.
  - An orange ring marks the thing each step talks about, with a card beside it.
  - Back, Next, Skip; arrow keys and Esc work too.
- **`docs/GUIDE.md`** is the same material as a document.
- **Reading level.** Both are written at or below a 5th-grade reading level: short sentences and
  everyday words. `test/reading-level.test.ts` checks that with Flesch-Kincaid (the tour and the guide
  as a whole must score 5.5 or lower).

## 12. Forbidden

- No shadows, gradients or blur.
- No weights above 500.
- No pure #fff or #000, except the 40% black backdrop behind the tour.
- No orange except for what's in play (§8).
- No default React Flow nodes, edges, controls or background.
- No emoji.
- No ellipsis on titles.
- A plain click in Select never creates or links.

## 13. Open questions

- Do `vscode://file/...` links open the file from inside VS Code's Simple Browser? They do from an
  ordinary browser. Needs checking on a Mac.
- One map per project, or several (`.dora/maps/*.json`)?
- Should link labels be editable on the canvas (select a link, press Enter)? Today only the AI sets them.
- Should workflow steps be editable after recording (reorder, remove)? Today you re-record, or the AI
  edits them.
- Publish to npm once public, or keep installing from GitHub?
