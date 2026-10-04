# Dora

A map that explains a product, built inside VS Code with your AI.

Pick a starter or press B and click to add boxes; hold Ctrl and click to chain them; open any card's
Details for the depth. Dora speaks the language enterprise products are explained in: where customers
meet the product, what data comes in and what it passes through, how it's secured, stored and
tracked, and what the customer ends up with. Group the paths into workflows, light one up, and present
it step by step. Flip to Blueprint to see the same map in lanes. Or ask your AI to map the project: it
writes the map file, and you watch the cards arrive and edit them alongside it.

New to it? Read [the guide](docs/GUIDE.md), or click the question mark in the app for the tour.

## Install once

You need [Node](https://nodejs.org) 20 or newer and git.

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
dora [folder] [--port 4317] [--open] [--no-skill] [--update-skill]
dora check [folder]      # what's wrong with the map; your AI runs this after editing
```

After updating Dora, run `dora --update-skill` in a project to give its AI the newest instructions
(Dora tells you when they're out of date).

## Controls

The left toolbar sets what a click does. Box, Chain and Insert stay on until you click them off or
press Esc.

| | |
|---|---|
| **V** Select | Click to select, Shift-click to add, drag to move, double-click open space for a box |
| **A** Area | Drag a box around cards to select them all |
| **B** Box | Every click in open space adds a box of the kind picked beside the toolbar |
| **C** Chain | Each card you click links from the last (same as holding Ctrl/Cmd) |
| **L** Link | One link (same as tapping Ctrl/Cmd) |
| **I** Insert | Click a link to put a new step in the middle |
| **H** Pan | Look around without changing anything |
| **W** Workflows | The paths customers take: make one, light it up, present it |
| **P** Present | Walk through a workflow one step at a time |
| Selection bar | With cards selected: color, kind, pin, details, delete, all at once |
| Pin (card corner) | Locks a card: it can't be dragged and Tidy leaves it |
| Double-click text | Edit the title, summary, details, who, system or files |
| **G** / **T** / **F** | Flow ↔ Blueprint / Tidy / Fit |
| Delete · Cmd/Ctrl+Z | Remove what's selected · undo (Shift to redo) |

The full rules, including spacing, colors and how the AI and canvas stay in sync, are in
[DESIGN.md](DESIGN.md).

## How the map is stored

```
.dora/map.json      events, links and workflows: you and your AI both edit this
.dora/layout.json   card positions, pins, open cards, Flow or Blueprint (canvas only)
.dora/changes.log   what was changed on the canvas, for the AI to read
```

## Develop

```sh
npm install
npm run dev          # canvas + API on http://127.0.0.1:5173, mapping ./playground
npm test             # map rules, spacing, dora check, the tour's reading level
npm run accept       # browser tests of every interaction (Playwright)
npm run typecheck
```

`DORA_DIR=/some/project npm run dev` points the dev server at another folder.

## License

MIT. See [LICENSE](LICENSE).
