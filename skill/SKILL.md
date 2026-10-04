---
name: dora
description: Read and edit this project's Dora map (.dora/map.json), the visual explanation of how the product works that the person views in VS Code. Use when the person asks to map, explain, diagram or walk through the project or a feature, to add, change or connect events or workflows on the map, or mentions Dora.
---
<!-- dora-skill-version: 2 -->

# Dora: the map that explains this product

The person keeps a map of this project in `.dora/`, open in a VS Code tab (Dora runs on localhost).
It explains the product the way enterprise products are explained:

- **who** does what;
- **what data** comes in and what it **passes through**;
- how it's **secured**, where it's **kept** and how it's **tracked**;
- what the customer **ends up with**.

The map is made of **events** (cards) joined by **links** (arrows, left to right), and **workflows**
(named paths through the events). You edit `.dora/map.json`; the person edits on the canvas. The
canvas reloads the moment you save, so they watch your changes arrive.

## Before you change anything

1. Read `.dora/map.json`.
2. Read the last 30 lines of `.dora/changes.log` if it exists. It lists what the person changed on
   the canvas, newest last. Build on their edits. Never put back what they removed, and never
   rewrite what they wrote unless they asked.

## The one rule that matters most

**An event is something that happens, or a thing data passes through, that a person would name.**
It is not a file, a class or a function. Files go in the event's `files` list.

| Bad (code structure) | Good (what happens) |
|---|---|
| `AuthController.ts` | Security: "Check who they are" |
| `utils/validate.ts` | Process: "Check and clean the data" |
| `models/Invoice` | Data: "Invoices" |
| `cron.ts` | Start: "Every night at 2am" |

## Kinds

Pick the kind that answers "what is this, to someone learning the product?"

| Kind | Use it for | Example title |
|---|---|---|
| `start` | What kicks off a workflow: a person, a schedule, an event from outside | Customer signs up · Every night at 2am · A file arrives |
| `touchpoint` | Where a person meets the product | Sign-up page · Mobile app · Public API · Weekly email |
| `action` | Something a person or the system does that isn't one of the kinds below | Customer invites teammates |
| `data` | A kind of data coming in or moving. When data splits by type, give each type its own card | Customer records · Payments · Uploaded files |
| `process` | Something data passes through that changes or judges it | Check and clean the data · Match to an account · Score the risk |
| `storage` | Where data is kept | Customer database · Data warehouse · File storage |
| `security` | A check things must pass | Sign-in (SSO) · Permission check · Encrypt at rest · Consent |
| `decision` | A fork where the path depends on a rule. It links to two or more events, with link labels | Approved or flagged? |
| `integration` | An outside system | Stripe · Salesforce · Okta |
| `tracking` | How things are recorded and watched | Audit log · Product analytics · Error alerts |
| `outcome` | What the customer ends up with. Every workflow should end in one | Dashboard is ready · Payment complete |
| `note` | Context that isn't a step. It needs no links | "Data is kept for 7 years for compliance" |

In Blueprint view, kinds sort into lanes: Customer (start, action, outcome), Touchpoints, Data,
Processing (process, decision), Security, Storage & systems (storage, integration), Tracking, and
Notes. If a lane would be empty for a product that clearly has that layer, you probably missed something.

## The file

```json
{
  "version": 1,
  "title": "How data moves through the product",
  "events": [
    {
      "id": "arrives",
      "kind": "start",
      "title": "New data arrives",
      "summary": "Data shows up from customers and partner systems, every few minutes.",
      "details": "## Why\n...\n## How it works\n...\n## Hands off to\n...",
      "actor": "System",
      "system": "Ingest service",
      "color": "",
      "files": ["services/ingest/receive.ts"]
    }
  ],
  "links": [{ "from": "arrives", "to": "payments", "label": "" }],
  "workflows": [
    { "id": "payment-report", "title": "Payment data becomes a report", "summary": "One kind of data, start to finish.", "steps": ["arrives", "payments", "clean", "access", "warehouse", "reports"] }
  ]
}
```

**Events**
- `id`: a short lowercase slug (`payments`, `sign-in`). Never change an existing id; links,
  workflows and the canvas's layout are keyed by it.
- `title`: a few words naming what happens. Under 60 characters.
- `summary`: the snapshot. One or two plain sentences on what it does, for someone who only skims.
  No jargon they'd have to look up.
- `details`: the depth, in markdown, shown when the card is opened: why it exists, how it works,
  edge cases, what it hands to the next event. Point at files instead of pasting code.
- `actor`: who does it ("Customer", "Admin", "Nightly job", "System"). `system`: what it runs on
  ("Okta", "Postgres", "Stripe"). Both are optional, but enterprise readers look for them first, so
  fill them in when you know them.
- `color`: optional: `blue`, `teal`, `green`, `purple`, `pink`, `red`, `yellow`, `slate`, or `""`.
  Use color to mean one thing across the map (for example, the team that owns it, or the phase it
  ships in) and say what it means in a `note`. Don't color everything.
- `files`: project-relative paths where this event lives in the code.

**Links**
- `from` comes before `to` in the story.
- No duplicates, no loops, and both ends must exist.
- `label` is optional and short. Use it on a decision's branches ("approved", "flagged") and where
  the reason for the step isn't obvious ("on failure", "async").

**Workflows**
- One path a customer (or the system) takes, start to finish: "New customer signs up",
  "Customer runs a report", "Admin adds a user".
- `steps` are event ids in order, and each step should link to the next.
- Shared pieces, like sign-in or the warehouse, appear once on the map, and several workflows run
  through them. Never copy an event to put it in a second workflow.

## Mapping a product from scratch

1. **Find the workflows first.** List the 2–6 main paths people take through the product.
2. **Walk each one in order:** start → touchpoint → data in → processing → security → storage →
   tracking → outcome. Not every workflow has every layer, but ask about each.
3. **Split data by type** where it really splits (records, payments, files), and join the paths
   again where they meet the same process.
4. **Reuse** events workflows share; don't duplicate them.
5. **Aim for 8–25 events.** For more than that, keep the top level coarse and put the depth in
   `details`.
6. **Give every event a summary,** and give the important ones details, actor, system and files.
7. **Add the workflows** to `workflows`.
8. **Run `dora check`** (see below) and fix everything under "Must fix", then what you can under
   "Worth fixing".
9. **Tell the person in one line** what you mapped and which workflow to look at first.

## Checking your work

After every edit, run this from the project folder:

```sh
dora check
```

It lists:
- **Must fix:** broken JSON (with the line), loops, links or workflow steps that point at nothing.
- **Worth fixing:** no start, missing summaries, unlinked events, long titles, workflow steps that
  aren't linked.

It exits 1 while anything must be fixed. If the canvas shows a red banner, the file didn't parse:
fix it before anything else.

## Rules

- Write the whole file as valid JSON (2-space indent). If it doesn't parse, the canvas keeps
  showing the last good version.
- Never edit `.dora/layout.json`. Positions are the canvas's job: new events are placed and spaced
  automatically, and cards the person pinned stay put.
- Keep fields you don't recognise; the person or another tool may have added them.
- Ask before rewriting an event the person edited (see changes.log) unless they asked you to.
