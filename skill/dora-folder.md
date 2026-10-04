# .dora: this project's Dora map

This folder is a map that explains this product: events (the pieces), links (what leads to what) and
workflows (the paths customers take). Open it by running `dora` in this folder; it shows at
http://localhost:4317.

- `map.json`: the content (events, links, workflows). People edit it on the canvas; AI assistants
  edit the file directly. Either way, the other side sees the change within a moment.
- `layout.json`: where each card sits, which cards are pinned or open, and the view (Flow or
  Blueprint). Only the canvas writes this.
- `changes.log`: one line per edit made on the canvas, newest last, so an AI can see what the person
  changed.

Commit this folder with the project, so the map travels with the code it explains.

The full format, and how to write a good map, is in `.claude/skills/dora/SKILL.md`. Run `dora check`
to see what's wrong with the map.
