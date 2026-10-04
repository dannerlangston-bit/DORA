# .dora — this project's Dora map

This folder is a map that explains this project: events (the pieces of the story) and links (what
leads to what). Open it with `dora` in this folder; it shows at http://localhost:4317.

- `map.json` — the content: events and links. People edit it on the canvas; AI assistants edit the
  file directly. Either way, the other side sees the change within a moment.
- `layout.json` — where each card sits, which cards are pinned and which are expanded. Only the
  canvas writes this.
- `changes.log` — one line per edit made on the canvas, newest last, so an AI can see what the person
  changed.

Commit this folder with the project so the map travels with the code it explains.

## map.json

```json
{
  "version": 1,
  "title": "How checkout works",
  "events": [
    {
      "id": "cart",
      "kind": "start",
      "title": "Customer opens the cart",
      "summary": "One or two plain sentences: what this does, at a glance.",
      "details": "Markdown, in depth: why it exists, how it works, what it hands to the next step.",
      "files": ["src/cart/CartPage.tsx"]
    }
  ],
  "links": [{ "from": "cart", "to": "pay", "label": "" }]
}
```

`kind` is one of `start`, `step`, `decision`, `milestone`, `note`. Links run one way and never loop.
