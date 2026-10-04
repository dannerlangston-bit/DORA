# Dora

A map that explains a project, built inside VS Code with your AI.

Start on a blank canvas. Click to add an event, hold Ctrl and click to chain events together, and
open any card's Details for the in-depth explanation. Or ask your AI to map the project: it writes
the map file, and you watch the cards arrive and edit them alongside it.

Dora is a sibling of LD3 Map and uses the same map engine. LD3 Map draws a project from a ledger of
work that already happened; Dora is for creating the map, to explain the project.

## Install once

You need Node 20 or newer.

```sh
git clone https://github.com/dannerlangston-bit/dora.git ~/tools/dora
cd ~/tools/dora
npm install      # also builds the canvas
npm link         # puts the `dora` command on your PATH
```

That's the only setup. To update later: `cd ~/tools/dora && git pull && npm install`.

## Use it in a project

1. Open the project in VS Code.
2. In the terminal: `dora`
3. Cmd/Ctrl+Shift+P, then **Simple Browser: Show**, then paste the URL it printed
   (`http://localhost:4317`).

The map is saved in the project's `.dora/` folder. Commit it with the project, so the map travels
with the code it explains. Each project has its own map, so there's nothing to clone or copy per
project. In a different project, run `dora` there.

The first run also adds `.claude/skills/dora/SKILL.md`, which tells Claude Code how to read and edit
the map. Ask it things like *"map this project in Dora"* or *"add the retry flow to the map."*

```
dora [folder] [--port 4317] [--open] [--no-skill]
```

## Controls

| | |
|---|---|
| Click empty space | New event. If an event is selected, the new one links from it |
| Type, Enter | Name it. Click away unnamed and it's gone |
| Click an event | Select it (orange) |
| **Hold Ctrl/Cmd and click** | Chain: each event you click links from the last. Let go to finish |
| **Tap Ctrl/Cmd**, then click | Link once, then back to normal |
| Double-click text | Edit the title, summary, details or files |
| Details | Open the in-depth explanation |
| Drag a card | Move it (it stays pinned there) |
| Delete | Remove the selected event or link |
| Cmd/Ctrl+Z | Undo (add Shift to redo) |
| T / F / L | Tidy / Fit / Link |

The full rules, including spacing, colors and how the AI and canvas stay in sync, are in
[DESIGN.md](DESIGN.md).

## How the map is stored

```
.dora/map.json      events and links: you and your AI both edit this
.dora/layout.json   card positions, pins, which cards are open (canvas only)
.dora/changes.log   what was changed on the canvas, for the AI to read
```

## Develop

```sh
npm install
npm run dev          # canvas + API on http://127.0.0.1:5173, mapping ./playground
npm test             # map rules and spacing
npm run accept       # browser tests of every interaction (Playwright)
npm run typecheck
```

`DORA_DIR=/some/project npm run dev` points the dev server at another folder.
