/**
 * The tour: how to use Dora, step by step. Written at or below a 5th-grade reading level (short
 * sentences, everyday words); test/reading-level.test.ts checks every step, and docs/GUIDE.md.
 *
 * `target` is the data-tour attribute of the thing to point at; none means a card in the middle.
 */
export type TourStep = { title: string; body: string; target?: string }

export const TOUR: TourStep[] = [
  {
    title: 'Welcome to Dora',
    body: 'Dora helps you draw a map of how a product works. Each box is one part of it. Lines show what comes next. This short tour shows you how to use it.',
  },
  {
    title: 'Make a box',
    target: 'tool-box',
    body: 'Click the Box button. Then click an empty spot on the map. A new box shows up. Type its name and press Enter. Box stays on until you click it again.',
  },
  {
    title: 'Pick what kind of box',
    target: 'tool-box',
    body: 'When Box is on, a list of kinds shows up next to it. Pick one, like Data or Security, before you click. The small picture on each box shows its kind.',
  },
  {
    title: 'Connect boxes',
    target: 'tool-chain',
    body: 'Hold down the Ctrl key. Click one box, then the next, then the next. Lines join them in order. Let go of Ctrl when you are done. On a Mac, Cmd works too.',
  },
  {
    title: 'Make just one line',
    target: 'tool-link',
    body: 'Tap Ctrl one time, or click Link. Then click a box. Dora draws one line to it. After that, things go back to normal.',
  },
  {
    title: 'Add a step in the middle',
    target: 'tool-insert',
    body: 'Forgot a step? Click Insert. Then click the line where the step goes. A new box fits right in.',
  },
  {
    title: 'Read and change a box',
    body: 'Each box shows a short summary. Click Details on a box to read more. Double-click any words to change them. Press Enter to save.',
  },
  {
    title: 'Pick many boxes',
    target: 'tool-area',
    body: 'Click Area. Then drag a square around some boxes. Now you can change them all at once. You can give them a color or a new kind.',
  },
  {
    title: 'Lock a box',
    body: 'Each box has a pin in its top right corner. Click the pin to lock the box in place. A locked box can not be moved. Click the pin again to unlock it.',
  },
  {
    title: 'Workflows',
    target: 'workflows',
    body: 'A workflow is one path through the map. It shows how a customer gets from start to finish. Open Workflows to make one. Click a workflow to light up its path.',
  },
  {
    title: 'Show it to someone',
    target: 'present',
    body: 'Click Present to walk through a workflow. Dora shows one step at a time. Use the arrow keys to go forward and back.',
  },
  {
    title: 'See it in rows',
    target: 'view',
    body: 'Click Blueprint to sort boxes into rows by what they do. Data goes in one row. Security goes in another. This makes a big product easy to read.',
  },
  {
    title: 'Keep it neat',
    target: 'tidy',
    body: 'Dora moves boxes so they stay neat. If things get messy, click Tidy. Locked boxes stay where they are.',
  },
  {
    title: 'Your AI helper can build too',
    body: 'Ask your AI helper to map your project. It writes the map for you. You will see the boxes show up here. You can change anything it makes.',
  },
  {
    title: 'You are ready',
    target: 'help',
    body: 'Click the question mark any time to see this tour again. Have fun making your map!',
  },
]
