import fs from 'node:fs'
import path from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { DoraMap } from '../shared/ops.js'

const WORK = path.join(import.meta.dirname, '.work', '.dora')
const read = (): DoraMap => JSON.parse(fs.readFileSync(path.join(WORK, 'map.json'), 'utf8'))
const links = () => read().links.map((l) => `${l.from}>${l.to}`).sort()
const ev = (id: string, title: string, extra: object = {}) => ({ id, kind: 'step', title, summary: '', details: '', files: [], ...extra })

function writeMap(events: object[], linkPairs: [string, string][] = []) {
  const map = { version: 1, title: 'Test map', events, links: linkPairs.map(([from, to]) => ({ from, to, label: '' })) }
  fs.writeFileSync(path.join(WORK, 'map.json'), JSON.stringify(map, null, 2))
}

async function open(page: Page, events: object[] = [], linkPairs: [string, string][] = []) {
  writeMap(events, linkPairs)
  fs.writeFileSync(path.join(WORK, 'layout.json'), JSON.stringify({ positions: {}, pinned: [], expanded: [] }))
  fs.rmSync(path.join(WORK, 'changes.log'), { force: true })
  await page.waitForTimeout(150) // let the watcher pick the file up before the page asks
  await page.goto('/')
  await expect(page.getByTestId('event')).toHaveCount(events.length)
  if (!events.length) await expect(page.getByTestId('empty')).toBeVisible()
  await page.waitForTimeout(400) // first layout settles
}

const card = (page: Page, id: string) => page.locator(`[data-event-id="${id}"]`)
const canvas = (page: Page) => page.getByTestId('canvas')

test('click empty space makes an event; type and Enter names it; the next click continues the story', async ({ page }) => {
  await open(page)
  await page.mouse.click(300, 300)
  await page.keyboard.type('Customer opens the cart')
  await page.keyboard.press('Enter')
  await page.mouse.click(760, 300)
  await page.keyboard.type('Prices recalculated')
  await page.keyboard.press('Enter')
  await expect.poll(() => read().events.map((e) => e.title)).toEqual(['Customer opens the cart', 'Prices recalculated'])
  const [a, b] = read().events
  expect(a.kind).toBe('start')
  expect(links()).toEqual([`${a.id}>${b.id}`])
  await expect(card(page, b.id)).toHaveAttribute('data-state', 'head')
  // auto-spacing puts the linked one a column to the right, 96px after the first card
  await expect.poll(async () => (await card(page, b.id).boundingBox())!.x - (await card(page, a.id).boundingBox())!.x).toBe(240 + 96)
})

test('a new event clicked away from unnamed disappears, and that click makes nothing else', async ({ page }) => {
  await open(page)
  await page.mouse.click(300, 300)
  await expect(page.getByTestId('event')).toHaveCount(1)
  await page.mouse.click(800, 500)
  await expect(page.getByTestId('event')).toHaveCount(0)
  await expect.poll(() => read().events.length).toBe(0)
})

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

test('Ctrl-click empty space while chaining adds the next event to the chain', async ({ page }) => {
  await open(page, [ev('a', 'A')])
  await page.keyboard.down('Control')
  await card(page, 'a').click()
  await page.mouse.click(900, 600)
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
  // back to normal: a plain click only selects
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

test('the AI writing map.json shows up on the canvas, and a broken file is called out', async ({ page }) => {
  await open(page, [ev('a', 'A')])
  writeMap([ev('a', 'A'), ev('b', 'Written by the AI', { summary: 'Appears without a reload.' })], [['a', 'b']])
  await expect(card(page, 'b')).toContainText('Written by the AI')
  await expect(card(page, 'b')).toContainText('Appears without a reload.')
  fs.writeFileSync(path.join(WORK, 'map.json'), '{ "version": 1, "events": [ oops ] }')
  await expect(page.getByTestId('file-banner')).toContainText('syntax error')
  await expect(page.getByTestId('event')).toHaveCount(2) // still the last good version
  writeMap([ev('a', 'A')])
  await expect(page.getByTestId('file-banner')).toHaveCount(0)
  await expect(page.getByTestId('event')).toHaveCount(1)
})

test('Details opens the in-depth part; editing it saves to the file and the change log', async ({ page }) => {
  await open(page, [ev('a', 'Checkout', { summary: 'Takes payment.', details: '## How\n- calls **Stripe**', files: ['src/pay.ts'] })])
  await expect(page.getByTestId('details')).toHaveCount(0)
  await card(page, 'a').getByTestId('details-toggle').click()
  await expect(page.getByTestId('details')).toContainText('calls Stripe')
  await expect(page.getByTestId('details')).toContainText('src/pay.ts')
  await page.getByTestId('details-body').dblclick()
  await page.getByTestId('details-body-input').fill('Rewritten on the canvas.')
  await page.keyboard.press('Control+Enter')
  await expect.poll(() => read().events[0].details).toBe('Rewritten on the canvas.')
  expect(fs.readFileSync(path.join(WORK, 'changes.log'), 'utf8')).toMatch(/user edited details of a "Checkout"/)
  await expect.poll(() => JSON.parse(fs.readFileSync(path.join(WORK, 'layout.json'), 'utf8')).expanded).toEqual(['a'])
})

test('Delete removes the selected event; Ctrl+Z brings it back with its links', async ({ page }) => {
  await open(page, [ev('a', 'A'), ev('b', 'B')], [['a', 'b']])
  await card(page, 'b').click()
  await page.keyboard.press('Delete')
  await expect(page.getByTestId('event')).toHaveCount(1)
  await expect.poll(() => read().events.length).toBe(1)
  await page.keyboard.press('Control+z')
  await expect(page.getByTestId('event')).toHaveCount(2)
  await expect.poll(links).toEqual(['a>b'])
})

test('dragging a card pins it; Tidy unpins', async ({ page }) => {
  await open(page, [ev('a', 'A'), ev('b', 'B')], [['a', 'b']])
  const box = (await card(page, 'b').boundingBox())!
  await page.mouse.move(box.x + 120, box.y + 20)
  await page.mouse.down()
  await page.mouse.move(box.x + 160, box.y + 260, { steps: 8 })
  await page.mouse.up()
  await expect(card(page, 'b').getByTestId('pinned')).toBeVisible()
  await page.getByTestId('tidy-button').click()
  await expect(card(page, 'b').getByTestId('pinned')).toHaveCount(0)
})
