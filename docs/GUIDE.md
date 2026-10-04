# How to use Dora

Dora helps you draw a map of how a product works. Each box is one part of it. Lines show what comes
next. You can build the map yourself, or ask your AI helper to build it with you.

## Get it running

You only set Dora up one time. Open the Terminal and type these lines:

```sh
git clone https://github.com/dannerlangston-bit/dora.git ~/tools/dora
cd ~/tools/dora
npm install
npm link
```

Now open a project in VS Code. In the VS Code terminal, type `dora`. Dora prints a web address.
Press Cmd+Shift+P (Ctrl+Shift+P on Windows) and pick **Simple Browser: Show**. Paste the address.
Your map opens in a tab, right next to your code.

The map is saved in a folder called `.dora` inside your project. Save it with the rest of your work.
Each project gets its own map.

## Start a map

When the map is empty, you can pick a starter. A starter gives you a few boxes to fill in. Or press
B and click anywhere to make your first box.

## Make a box

1. Click the **Box** button on the left. It stays on until you click it again.
2. Click an empty spot. A new box shows up.
3. Type a name and press Enter.

If a box is picked when you click, the new box links to it. So you can click, type, click, type, and
build a whole path fast.

You can also double-click an empty spot to make a box.

## Kinds of boxes

When Box is on, a list of kinds shows up. Pick one before you click. The small picture on a box
shows its kind. You can change it later.

| Kind | What it means |
|---|---|
| Start | What gets things going. A customer signs up, or a file arrives. |
| Touchpoint | Where a person uses the product, like a web page or an app. |
| Action | Something a person or the computer does. |
| Data | A kind of information that comes in or moves around. |
| Process | A step that checks or changes the data. |
| Storage | Where the data is kept. |
| Security | A check that keeps things safe, like signing in. |
| Decision | A spot where the path splits in two or more ways. |
| Integration | Another company's system that the product talks to. |
| Tracking | How the product keeps records and watches what happens. |
| Outcome | What the customer gets at the end. |
| Note | Extra facts that are not a step. |

## Connect boxes

- **Many lines at once:** Hold down Ctrl. Click one box, then the next, then the next. Lines join
  them in order. Let go of Ctrl when you are done. On a Mac, Cmd works too.
- **Just one line:** Tap Ctrl one time, or click **Link**. Then click a box. Dora draws one line to it.
- **Chain:** Click **Chain** to keep linking without holding a key. Press Esc to stop.
- **Add a step in the middle:** Click **Insert**. Then click the line where the step goes.

Lines always go one way. Dora will not make a loop.

## Read and change a box

Each box shows a short summary. Click **Details** to read more. Double-click any words to change
them. Press Enter to save. In the details, press Cmd+Enter (Ctrl+Enter on Windows) to save.

Details also have **Who** (who does it) and **System** (what it runs on). They show up under the
title.

## Pick many boxes

Click **Area**. Then drag a square around some boxes. You can also hold Shift and click boxes one
at a time.

A bar shows up at the top. Use it to change all the boxes at once. You can:
- give them a color;
- change their kind;
- pin them;
- open or close their details;
- delete them.

## Lock a box

Each box has a pin in its top right corner. Click the pin to lock the box. A locked box can not be
moved. Click the pin again to unlock it.

## Move around

Drag an empty spot to move the map. Scroll to move up and down. Hold Ctrl and scroll to zoom. Click
**Pan** if you only want to look around. Click **Fit** to see the whole map.

## Workflows

A workflow is one path through the map. It shows how a customer gets from start to finish.

1. Click the **Workflows** button on the left.
2. Click **New workflow** and type a name.
3. Click the boxes in order. Dora adds any missing lines.
4. Click **Done**.

Click a workflow in the list to light up its path. The other boxes fade.

## Show it to someone

Click **Present**. Dora walks through a workflow one step at a time. Use the arrow keys or the
buttons to go forward and back. Press Esc to stop.

## See it in rows

Click **Blueprint** at the bottom right. Dora sorts the boxes into rows by what they do. Data goes in
one row. Security goes in another. Click **Flow** to go back.

## Keep it neat

Dora moves boxes so they stay neat. If you drag a box, it stays where you put it. If things get
messy, click **Tidy**. Locked boxes stay where they are.

Made a mistake? Press Cmd+Z (Ctrl+Z on Windows) to undo.

## Let your AI helper build

Ask your AI helper to map your project. For example: "Map this project in Dora." It writes the map
for you. You will see the boxes show up as it works. You can change anything it makes, and it can
see what you change.

## Keys

| Key | What it does |
|---|---|
| V | Select |
| A | Area: pick many boxes |
| B | Box: make boxes |
| C | Chain: link boxes in a row |
| L | Link: make one line |
| I | Insert: add a step in the middle |
| H | Pan: look around |
| W | Open Workflows |
| P | Present |
| G | Switch Flow and Blueprint |
| T | Tidy |
| F | Fit the map on the screen |
| Esc | Stop what you are doing |
| Delete | Delete what is picked |

Click the question mark at the bottom right any time to take the tour again.
