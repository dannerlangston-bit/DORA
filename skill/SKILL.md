---
name: dora
description: Read and edit this project's Dora map (.dora/map.json), the visual explanation of how the project works that the person views in VS Code. Use when the person asks to map, explain, diagram or walk through the project, to add, change or connect events on the map, or mentions Dora.
---

# Dora: the map that explains this project

The person keeps a map of this project in `.dora/`, open in a VS Code tab (Dora runs on localhost).
It is a story told in **events** (cards) joined by **links** (arrows, left to right). You and the
person edit the same map: you edit `.dora/map.json`; they edit on the canvas. The canvas reloads
the moment you save, so they watch your changes arrive.

## Before you change anything

1. Read `.dora/map.json`.
2. Read the last 30 lines of `.dora/changes.log` (if it exists). It lists what the person changed on
   the canvas, newest last. Respect those edits: if they renamed, rewrote, linked or deleted
   something, build on it. Never put back what they removed.

## The file

```json
{
  "version": 1,
  "title": "How checkout works",
  "events": [
    {
      "id": "cart",
      "kind": "start",
      "title": "Customer opens the cart",
      "summary": "The cart page loads saved items and re-prices them before showing anything.",
      "details": "## Why\n...\n## How it works\n...\n## Hands off to\n...",
      "files": ["src/cart/CartPage.tsx", "src/cart/pricing.ts"]
    }
  ],
  "links": [{ "from": "cart", "to": "pay", "label": "" }]
}
```

- `id`: a short lowercase slug (`cart`, `pay-intent`). Never change an existing id: links and the
  canvas's layout are keyed by it.
- `kind`: `start` (where the story begins, usually one), `step`, `decision` (a fork: one event links
  to several), `milestone` (a result someone would point at), `note` (context, not a step).
- `title`: a few words naming the event. Shown in full on the card, up to two lines.
- `summary`: the snapshot, one or two plain sentences on what it does. Someone skimming the map
  reads only this. No jargon they would have to look up.
- `details`: the in-depth explanation, in markdown, shown when the card is expanded. Why it exists,
  how it works, edge cases, what it hands to the next event. Point at files instead of pasting code.
- `files`: project-relative paths where this event lives in the code.
- `links`: `from` comes before `to` in the story. No duplicates, no loops, both ends must exist.
  `label` is optional and short ("on failure", "async").

## Rules

- Write the whole file as valid JSON (2-space indent). If it doesn't parse, the canvas keeps
  showing the last good version and tells the person the file is broken.
- Never edit `.dora/layout.json`. Positions are the canvas's job: new events are placed and spaced
  automatically.
- Keep fields you don't recognise; the person or another tool may have added them.
- Ask before rewriting an event the person edited (see changes.log) unless they asked you to.

## Mapping a project from scratch

When asked to "map this project" (or similar):

1. Read the code enough to know its main flow: entry points, the core path a user or request takes,
   where it branches, what it produces.
2. Start with one `start` event, then the main path left to right, then the branches.
3. Aim for 8–20 events. More than that, split it into several groups (unlinked chains) or keep the
   top level coarse and put the depth in `details`.
4. Every event gets a summary; give the important ones details and files.
5. Tell the person in one line what you mapped and what to look at.
