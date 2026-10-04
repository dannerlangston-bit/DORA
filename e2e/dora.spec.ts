import fs from 'node:fs'
import path from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { DoraMap } from '../shared/ops.js'

const WORK = path.join(import.meta.dirname, '.work', '.dora')
const read = (): DoraMap => JSON.parse(fs.readFileSync(path.join(WORK, 'map.json'), 'utf8'))
const layout = () => JSON.parse(fs.readFileSync(path.join(WORK, 'layout.json'), 'utf8'))
const links = () => read().links.map((l) => `${l.from}>${l.to}`).sort()
const ev = (id: string, title: string, extra: object = {}) => ({ id, kind: 'action', title, summary: '', details: '', files: [], ...extra })

function writeMap(events: object[], linkPairs: [string, string][] = [], workflows: object[] = []) {
  const map = { version: 1, title: 'Test map', events, links: linkPairs.map(([from, to]) => ({ from, to, label: '' })), workflows }
  fs.writeFileSync(path.join(WORK, 'map.json'), JSON.stringify(map, null, 2))
}

async function open(page: Page, events: object[] = [], linkPairs: [string, string][] = [], workflows: object[] = [], lay: object = {}) {
  writeMap(events, linkPairs, workflows)
  fs.writeFileSync(path.join(WORK, 'layout.json'), JSON.stringify({ version: 2, view: 'flow', positions: {}, placed: [], pinned: [], expanded: [], ...lay }))
  fs.rmSync(path.join(WORK, 'changes.log'), { force: true })
  await page.waitForTimeout(150) // let the watcher pick the file up before the page asks
  await page.goto('/')
  await expect(page.getByTestId('event')).toHaveCount(events.length)
  if (!events.length) await expect(page.getByTestId('empty')).toBeVisible()
  await page.waitForTimeout(400) // first layout settles
}

const card = (page: Page, id: string) => page.locator(`[data-event-id="${id}"]`)
const canvas = (page: Page) => page.getByTestId('canvas')
const box = async (page: Page, id: string) => (await card(page, id).boundingBox())!

test.describe('making boxes', () => {
  test('Box tool: click open space makes a box, Enter names it, the next click continues the story, and it stays on', async ({ page }) => {
    await open(page)
    await page.getByTestId('rail').locator('[data-tool=box]').click()
    await expect(page.getByTestId('kind-palette')).toBeVisible()
    await page.mouse.click(400, 300)
    await page.keyboard.type('Customer opens the cart')
    await page.keyboard.press('Enter')
    await page.mouse.click(860, 300)
    await page.keyboard.type('Prices recalculated')
    await page.keyboard.press('Enter')
    await expect.poll(() => read().events.map((e) => e.title)).toEqual(['Customer opens the cart', 'Prices recalculated'])
    const [a, b] = read().events
    expect(a.kind).toBe('start') // the first box of a map starts it
    expect(links()).toEqual([`${a.id}>${b.id}`])
    await expect(card(page, b.id)).toHaveAttribute('data-state', 'head')
    await expect(canvas(page)).toHaveAttribute('data-tool', 'box')
    // auto-spacing puts the linked one a column to the right, 96px after the first card
    await expect.poll(async () => (await box(page, b.id)).x - (await box(page, a.id)).x).toBe(240 + 96)
    // clicking Box again turns it off
    await page.getByTestId('rail').locator('[data-tool=box]').click()
    await expect(canvas(page)).toHaveAttribute('data-tool', 'select')
  })

  test('the kind picked in the palette is the kind of the next box', async ({ page }) => {
    await open(page, [ev('a', 'A')])
    await page.keyboard.press('b')
    await page.getByTestId('kind-palette').locator('[data-kind=security]').click()
    await page.mouse.click(900, 600)
    await page.keyboard.type('Check who they are')
    await page.keyboard.press('Enter')
    await expect.poll(() => read().events.find((e) => e.title === 'Check who they are')?.kind).toBe('security')
    await expect(page.locator('[data-kind=security][data-testid=event]')).toHaveCount(1)
  })

  test('Select: clicking open space only deselects; double-click makes a box', async ({ page }) => {
    await open(page, [ev('a', 'A')])
    await card(page, 'a').click()
    await expect(card(page, 'a')).toHaveAttribute('data-state', 'head')
    await page.mouse.click(900, 600)
    await expect(card(page, 'a')).not.toHaveAttribute('data-state', /.+/)
    await page.waitForTimeout(200)
    expect(read().events).toHaveLength(1)
    await page.mouse.dblclick(900, 600)
    await page.keyboard.type('Made by double-click')
    await page.keyboard.press('Enter')
    await expect.poll(() => read().events.map((e) => e.title)).toContain('Made by double-click')
  })

  test('a new box clicked away from unnamed disappears, and that click makes nothing else', async ({ page }) => {
    await open(page)
    await page.keyboard.press('b')
    await page.mouse.click(400, 300)
    await expect(page.getByTestId('event')).toHaveCount(1)
    await page.mouse.click(900, 600)
    await expect(page.getByTestId('event')).toHaveCount(0)
    await expect.poll(() => read().events.length).toBe(0)
  })

  test('starters fill an empty map', async ({ page }) => {
    await open(page)
    await page.getByTestId('starter-pipeline').click()
    await expect(page.getByTestId('event')).toHaveCount(9)
    await expect.poll(() => read().workflows.map((w) => w.title)).toEqual(['Payment data becomes a report'])
    expect(read().events.filter((e) => e.kind === 'data')).toHaveLength(3)
  })
})

test.describe('linking', () => {
  test('hold Ctrl: each click links from the last, all lit; letting go ends the chain', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B'), ev('c', 'C')])
    await page.keyboard.down('Control')
    await card(page, 'a').click()
    await card(page, 'b').click()
    await card(page, 'c').click()
    await expect(card(page, 'a')).toHaveAttribute('data-state', 'chain')
    await expect(card(page, 'b')).toHaveAttribute('data-state', 'chain')
    await expect(card(page, 'c')).toHaveAttribute('data-state', 'head')
    await expect(page.getByTestId('hint')).toContainText('Chaining')
    await page.keyboard.up('Control')
    await expect(canvas(page)).not.toHaveAttribute('data-chain', /.+/)
    await expect(card(page, 'a')).not.toHaveAttribute('data-state', /.+/)
    await expect(card(page, 'c')).toHaveAttribute('data-state', 'head')
    await expect.poll(links).toEqual(['a>b', 'b>c'])
  })

  test('Chain tool does the same without holding a key, until Esc', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B'), ev('c', 'C')])
    await page.keyboard.press('c')
    await card(page, 'a').click()
    await card(page, 'b').click()
    await card(page, 'c').click()
    await expect.poll(links).toEqual(['a>b', 'b>c'])
    await page.keyboard.press('Escape')
    await expect(canvas(page)).toHaveAttribute('data-tool', 'select')
  })

  test('Ctrl-click open space while chaining adds the next box to the chain', async ({ page }) => {
    await open(page, [ev('a', 'A')])
    await page.keyboard.down('Control')
    await card(page, 'a').click()
    await page.mouse.click(900, 650)
    await page.mouse.click(900, 200)
    await page.keyboard.up('Control')
    await expect(page.getByTestId('event')).toHaveCount(3)
    await expect.poll(() => read().events.length).toBe(3)
    const ids = read().events.map((e) => e.id)
    await expect.poll(links).toEqual([`${ids[0]}>${ids[1]}`, `${ids[1]}>${ids[2]}`].sort())
  })

  test('tap Ctrl: arms exactly one link, then back to normal', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B'), ev('c', 'C')])
    await card(page, 'a').click()
    await page.keyboard.press('Control')
    await expect(canvas(page)).toHaveAttribute('data-armed', '1')
    await expect(page.getByTestId('hint')).toContainText('Linking')
    await card(page, 'b').click()
    await expect(canvas(page)).not.toHaveAttribute('data-armed', '1')
    await expect.poll(links).toEqual(['a>b'])
    await card(page, 'c').click()
    await expect(card(page, 'c')).toHaveAttribute('data-state', 'head')
    await page.waitForTimeout(200)
    expect(links()).toEqual(['a>b'])
  })

  test('tap Ctrl with nothing selected: first click picks the start, second the end', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B')])
    await page.keyboard.press('Control')
    await card(page, 'b').click()
    await expect(canvas(page)).toHaveAttribute('data-armed', '1')
    await card(page, 'a').click()
    await expect.poll(links).toEqual(['b>a'])
  })

  test('Ctrl+Z is not a tap', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B')])
    await page.keyboard.press('Control+z')
    await expect(canvas(page)).not.toHaveAttribute('data-armed', '1')
  })

  test('a link that would make a loop is refused and says why', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B')], [['a', 'b']])
    await page.keyboard.down('Control')
    await card(page, 'b').click()
    await card(page, 'a').click()
    await page.keyboard.up('Control')
    await expect(page.getByTestId('notice')).toContainText('loop')
    expect(links()).toEqual(['a>b'])
  })

  test('Insert: click a link to put a new step in the middle, in workflows too', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B')], [['a', 'b']], [{ id: 'w', title: 'W', steps: ['a', 'b'] }])
    await page.keyboard.press('i')
    const A = await box(page, 'a'), B = await box(page, 'b')
    await page.mouse.click((A.x + A.width + B.x) / 2, A.y + A.height / 2)
    await page.keyboard.type('Middle')
    await page.keyboard.press('Enter')
    await expect.poll(() => read().events.find((e) => e.title === 'Middle')?.id).toBeTruthy()
    const mid = read().events.find((e) => e.title === 'Middle')!.id
    expect(links()).toEqual([`${mid}>b`, `a>${mid}`].sort())
    expect(read().workflows[0].steps).toEqual(['a', mid, 'b'])
  })
})

test.describe('many cards at once', () => {
  test('Area: drag a box to select cards, then color them all', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B'), ev('c', 'C')], [['a', 'b'], ['b', 'c']])
    const A = await box(page, 'a'), B = await box(page, 'b')
    await page.keyboard.press('a')
    await page.mouse.move(A.x - 20, A.y - 20)
    await page.mouse.down()
    await page.mouse.move(B.x + B.width + 20, B.y + B.height + 20, { steps: 8 })
    await expect(page.getByTestId('marquee')).toBeVisible()
    await page.mouse.up()
    await expect(canvas(page)).toHaveAttribute('data-selected', 'a b')
    await expect(page.getByTestId('selection-count')).toHaveText('2 selected')
    await page.getByTestId('selection-bar').locator('[data-color=teal]').click()
    await expect.poll(() => read().events.map((e) => e.color)).toEqual(['teal', 'teal', ''])
    await expect(card(page, 'a').getByTestId('color-stripe')).toBeVisible()
  })

  test('Shift-click adds to the selection; change the kind and delete them together', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B'), ev('c', 'C')])
    await card(page, 'a').click()
    await card(page, 'c').click({ modifiers: ['Shift'] })
    await expect(page.getByTestId('selection-count')).toHaveText('2 selected')
    await page.getByTestId('bulk-kind').selectOption('storage')
    await expect.poll(() => read().events.map((e) => e.kind)).toEqual(['storage', 'action', 'storage'])
    await page.getByTestId('bulk-delete').click()
    await expect(page.getByTestId('event')).toHaveCount(1)
    await page.keyboard.press('Control+z')
    await expect(page.getByTestId('event')).toHaveCount(3)
  })

  test('the pin locks a card: it can’t be dragged, and Tidy leaves it', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B')], [['a', 'b']])
    await card(page, 'b').getByTestId('pin').click()
    await expect(card(page, 'b')).toHaveAttribute('data-pinned', '1')
    await expect.poll(() => layout().pinned).toEqual(['b'])
    const before = await box(page, 'b')
    await page.mouse.move(before.x + 120, before.y + 40)
    await page.mouse.down()
    await page.mouse.move(before.x + 160, before.y + 300, { steps: 8 })
    await page.mouse.up()
    expect(await box(page, 'b')).toEqual(before)
    await page.getByTestId('tidy-button').click()
    await page.waitForTimeout(500)
    expect(await box(page, 'b')).toEqual(before)
    // unpinned, it moves again
    await card(page, 'b').getByTestId('pin').click()
    await page.mouse.move(before.x + 120, before.y + 40)
    await page.mouse.down()
    await page.mouse.move(before.x + 160, before.y + 300, { steps: 8 })
    await page.mouse.up()
    await expect.poll(async () => (await box(page, 'b')).y).toBeGreaterThan(before.y + 100)
  })
})

test.describe('cards', () => {
  test('Details opens the depth; editing it, who and system saves to the file and the change log', async ({ page }) => {
    await open(page, [ev('a', 'Checkout', { summary: 'Takes payment.', details: '## How\n- calls **Stripe**', files: ['src/pay.ts'] })])
    await expect(page.getByTestId('details')).toHaveCount(0)
    await card(page, 'a').getByTestId('details-toggle').click()
    await expect(page.getByTestId('details')).toContainText('calls Stripe')
    await expect(page.getByTestId('details')).toContainText('src/pay.ts')
    await page.getByTestId('details-body').dblclick()
    await page.getByTestId('details-body-input').fill('Rewritten on the canvas.')
    await page.keyboard.press('Control+Enter')
    await expect.poll(() => read().events[0].details).toBe('Rewritten on the canvas.')
    await page.getByTestId('system').dblclick()
    await page.keyboard.type('Stripe')
    await page.keyboard.press('Enter')
    await expect.poll(() => read().events[0].system).toBe('Stripe')
    await expect(card(page, 'a').getByTestId('who')).toHaveText('Stripe')
    expect(fs.readFileSync(path.join(WORK, 'changes.log'), 'utf8')).toMatch(/user edited details of a "Checkout"/)
    await expect.poll(() => layout().expanded).toEqual(['a'])
  })

  test('Delete removes the selected card; Ctrl+Z brings it back with its links', async ({ page }) => {
    await open(page, [ev('a', 'A'), ev('b', 'B')], [['a', 'b']])
    await card(page, 'b').click()
    await page.keyboard.press('Delete')
    await expect(page.getByTestId('event')).toHaveCount(1)
    await expect.poll(() => read().events.length).toBe(1)
    await page.keyboard.press('Control+z')
    await expect(page.getByTestId('event')).toHaveCount(2)
    await expect.poll(links).toEqual(['a>b'])
  })

  test('the AI writing map.json shows up on the canvas, and a broken file is called out', async ({ page }) => {
    await open(page, [ev('a', 'A')])
    writeMap([ev('a', 'A'), ev('b', 'Written by the AI', { summary: 'Appears without a reload.', kind: 'storage', system: 'Postgres' })], [['a', 'b']])
    await expect(card(page, 'b')).toContainText('Written by the AI')
    await expect(card(page, 'b')).toContainText('Postgres')
    fs.writeFileSync(path.join(WORK, 'map.json'), '{ "version": 1,\n  "events": [ oops ] }')
    await expect(page.getByTestId('file-banner')).toContainText('line 2')
    await expect(page.getByTestId('event')).toHaveCount(2)
    writeMap([ev('a', 'A')])
    await expect(page.getByTestId('file-banner')).toHaveCount(0)
    await expect(page.getByTestId('event')).toHaveCount(1)
  })

  test('maps made before the new kinds still open', async ({ page }) => {
    await open(page, [ev('a', 'A', { kind: 'step' }), ev('b', 'B', { kind: 'milestone' })], [['a', 'b']])
    await expect(card(page, 'a')).toHaveAttribute('data-kind', 'action')
    await expect(card(page, 'b')).toHaveAttribute('data-kind', 'outcome')
  })
})

test.describe('workflows, present, blueprint', () => {
  const story = [ev('s', 'Signs up', { kind: 'start' }), ev('d', 'Account details', { kind: 'data' }), ev('sec', 'Check who they are', { kind: 'security' }), ev('o', 'Ready', { kind: 'outcome' }), ev('x', 'Elsewhere')]
  const storyLinks: [string, string][] = [['s', 'd'], ['d', 'sec'], ['sec', 'o'], ['s', 'x']]

  test('record a workflow by clicking its steps; clicking it lights its path', async ({ page }) => {
    await open(page, story, storyLinks)
    await page.keyboard.press('w')
    await page.getByTestId('new-workflow').click()
    await page.getByTestId('workflow-name').fill('New customer signs up')
    await page.keyboard.press('Enter')
    for (const id of ['s', 'd', 'sec', 'o']) await card(page, id).getByTestId('title').click()
    await expect(canvas(page)).toHaveAttribute('data-recording', 's d sec o')
    await page.getByTestId('recording-done').click()
    await expect.poll(() => read().workflows.map((w) => [w.title, w.steps.join(' ')])).toEqual([['New customer signs up', 's d sec o']])
    await expect(canvas(page)).toHaveAttribute('data-focused', /.+/)
    await expect(card(page, 'x')).toHaveClass(/opacity-35/)
    await expect(card(page, 'd')).not.toHaveClass(/opacity-35/)
  })

  test('Present walks a workflow one step at a time', async ({ page }) => {
    await open(page, story, storyLinks, [{ id: 'w', title: 'Sign-up', steps: ['s', 'd', 'sec', 'o'] }])
    await page.keyboard.press('p')
    await expect(page.getByTestId('present-step')).toHaveText('Step 1 of 4')
    await expect(page.getByTestId('present-title')).toHaveText('Signs up')
    await page.keyboard.press('ArrowRight')
    await page.keyboard.press('ArrowRight')
    await expect(page.getByTestId('present-title')).toHaveText('Check who they are')
    await expect(card(page, 'sec')).toHaveAttribute('data-state', 'head')
    await page.keyboard.press('ArrowLeft')
    await expect(page.getByTestId('present-step')).toHaveText('Step 2 of 4')
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('present')).toHaveCount(0)
  })

  test('Blueprint puts kinds in lanes; Flow comes back where it was', async ({ page }) => {
    await open(page, story, storyLinks)
    const rounded = async (id: string) => { const b = await box(page, id); return [Math.round(b.x), Math.round(b.y)] }
    const flowS = await rounded('s')
    await page.getByTestId('view-blueprint').click()
    await expect(page.getByTestId('lane-label')).toHaveCount(3) // customer (start, action, outcome), data, security
    await expect(page.getByTestId('lane-label').first()).toContainText('Customer')
    await expect.poll(async () => (await box(page, 'd')).y > (await box(page, 's')).y + 100).toBe(true)
    await expect.poll(async () => (await box(page, 'sec')).y > (await box(page, 'd')).y + 100).toBe(true)
    await expect.poll(() => layout().view).toBe('blueprint')
    await page.getByTestId('view-flow').click()
    await expect.poll(() => rounded('s')).toEqual(flowS)
  })
})

test('the tour opens from Help and steps through', async ({ page }) => {
  await open(page, [ev('a', 'A')])
  await page.getByTestId('help-button').click()
  await page.getByTestId('tour-button').click()
  await expect(page.getByTestId('tour-title')).toHaveText('Welcome to Dora')
  await page.getByTestId('tour-next').click()
  await expect(page.getByTestId('tour-title')).toHaveText('Make a box')
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('tour')).toHaveCount(0)
})
