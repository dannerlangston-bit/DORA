# Dora: agent context

Dora is a canvas for building a map that explains a project, run from VS Code. It's a sibling of LD3
Map (same map engine and look), scoped down to creating maps.

- **The rules live in `DESIGN.md`.** Follow them; they are constraints. Every interaction rule there
  has a browser test in `e2e/dora.spec.ts`. Change the doc, the code and the test together.
- **One set of rules for both sides.** The map format and what counts as a valid edit live in
  `shared/ops.js`, used by both the server and the canvas. Never duplicate that logic.
- **The server** is plain Node (`server/dora-server.js`, `bin/dora.js`), JS with JSDoc types and no
  build step, so `npm link` and `npx` installs run it as is.
- **The canvas** is React with `@xyflow/react` and ELK for spacing (`src/canvas/`).
- **What ships to every project** is `skill/SKILL.md` (the AI's instructions) and
  `skill/dora-folder.md` (the `.dora/README.md`).

Before pushing, run `npm run typecheck && npm test && npm run build && npm run accept`.
