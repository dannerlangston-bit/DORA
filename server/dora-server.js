// @ts-check
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { jsonErrorLine } from '../shared/jsonpos.js'
import { applyOps, describeOp, emptyMap, normalizeMap, OpError } from '../shared/ops.js'

/** @typedef {import('../shared/ops.js').DoraMap} DoraMap */
/** @typedef {import('../shared/ops.js').Op} Op */
/**
 * Where cards sit. `placed`: cards a person put somewhere by hand; auto-spacing leaves them, Tidy
 * re-spaces them. `pinned`: cards locked in place; they can't be dragged and Tidy leaves them too.
 * @typedef {{ version: 2, view: 'flow' | 'blueprint', positions: Record<string, { x: number, y: number }>, placed: string[], pinned: string[], expanded: string[] }} Layout
 */

const PKG_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const DIST = path.join(PKG_ROOT, 'dist')
const MAP = 'map.json'
const LAYOUT = 'layout.json'
const LOG = 'changes.log'
const LOG_MAX = 2000
const LOG_KEEP = 1000

/** @returns {Layout} */
export const emptyLayout = () => ({ version: 2, view: 'flow', positions: {}, placed: [], pinned: [], expanded: [] })

/**
 * Read a layout file leniently. Version 1 used `pinned` for cards placed by hand (there was no
 * lock yet), so those become `placed`.
 * @param {any} raw @returns {Layout}
 */
export function normalizeLayout(raw) {
  const l = raw && typeof raw === 'object' ? raw : {}
  const list = (/** @type {unknown} */ v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [])
  const v1 = l.version !== 2
  return {
    version: 2,
    view: l.view === 'blueprint' ? 'blueprint' : 'flow',
    positions: l.positions && typeof l.positions === 'object' ? l.positions : {},
    placed: v1 ? list(l.pinned) : list(l.placed),
    pinned: v1 ? [] : list(l.pinned),
    expanded: list(l.expanded),
  }
}

/**
 * Make `.dora/` inside `dir` if it isn't there. Returns what was created, for the CLI to report.
 * @param {string} dir
 */
export function ensureWorkspace(dir) {
  const root = path.join(dir, '.dora')
  /** @type {string[]} */
  const created = []
  if (!fs.existsSync(root)) { fs.mkdirSync(root, { recursive: true }); created.push('.dora/') }
  const write = (/** @type {string} */ name, /** @type {string} */ text) => {
    const file = path.join(root, name)
    if (fs.existsSync(file)) return
    fs.writeFileSync(file, text)
    created.push(`.dora/${name}`)
  }
  write(MAP, json(emptyMap(path.basename(path.resolve(dir)))))
  write(LAYOUT, json(emptyLayout()))
  write('README.md', fs.readFileSync(path.join(PKG_ROOT, 'skill', 'dora-folder.md'), 'utf8'))
  return { root, created }
}

/** @param {unknown} v */
const json = (v) => `${JSON.stringify(v, null, 2)}\n`

/** Write via a temp file and rename, so the AI never reads a half-written map. */
function writeAtomic(/** @type {string} */ file, /** @type {string} */ text) {
  const tmp = `${file}.${process.pid}.tmp`
  fs.writeFileSync(tmp, text)
  fs.renameSync(tmp, file)
}

/**
 * Where a JSON syntax error sits, in words a person can act on.
 * @param {string} raw @param {unknown} err
 */
function syntaxMessage(raw, err) {
  void err
  const line = jsonErrorLine(raw)
  return `map.json has a JSON syntax error${line ? ` on line ${line}` : ''}. Showing the last version that worked until it's fixed.`
}

/**
 * One Dora workspace: the `.dora/` folder inside `dir`. Answers the canvas's API, watches the folder
 * for edits made by the AI (or anything else), and pushes every change to open canvases over
 * server-sent events.
 * @param {{ dir: string }} opts
 */
export function createDora({ dir }) {
  const { root } = ensureWorkspace(dir)
  const name = path.basename(path.resolve(dir))
  const mapFile = path.join(root, MAP)
  const layoutFile = path.join(root, LAYOUT)
  const logFile = path.join(root, LOG)

  let rev = 0
  /** @type {{ map: DoraMap, problems: string[], error: string | null }} */
  let current = { map: emptyMap(name), problems: [], error: null }
  /** raw text of the file as last read or written, so the watcher can skip its own echo */
  let lastRaw = ''
  /** @type {Set<http.ServerResponse>} */
  const clients = new Set()

  /** Read map.json from disk. On a syntax error, keep the last good map and say so. */
  function readMap() {
    const raw = fs.existsSync(mapFile) ? fs.readFileSync(mapFile, 'utf8') : ''
    if (!raw.trim()) return { raw, map: emptyMap(name), problems: [], error: null }
    try {
      return { raw, ...normalizeMap(JSON.parse(raw), name), error: null }
    } catch (err) {
      return { raw, map: current.map, problems: current.problems, error: syntaxMessage(raw, err) }
    }
  }

  function readLayout() {
    try {
      return normalizeLayout(JSON.parse(fs.readFileSync(layoutFile, 'utf8')))
    } catch {
      return emptyLayout()
    }
  }

  function snapshot() {
    return { rev, map: current.map, problems: current.problems, error: current.error }
  }

  /** @param {string} source */
  function broadcast(source) {
    const data = `event: state\ndata: ${JSON.stringify({ ...snapshot(), source })}\n\n`
    for (const res of clients) res.write(data)
  }

  /** @param {string[]} lines */
  function appendLog(lines) {
    if (!lines.length) return
    const stamp = new Date().toISOString().slice(0, 19) + 'Z'
    fs.appendFileSync(logFile, lines.map((l) => `${stamp} user ${l}\n`).join(''))
    const all = fs.readFileSync(logFile, 'utf8').split('\n')
    if (all.length > LOG_MAX) fs.writeFileSync(logFile, all.slice(-LOG_KEEP).join('\n'))
  }

  function reload() {
    const next = readMap()
    if (next.raw === lastRaw) return
    lastRaw = next.raw
    current = { map: next.map, problems: next.problems, error: next.error }
    rev++
    broadcast('file')
  }

  {
    const first = readMap()
    lastRaw = first.raw
    current = { map: first.map, problems: first.problems, error: first.error }
  }

  // fs.watch reports bursts (and sometimes no filename), so settle for a moment and re-read.
  /** @type {NodeJS.Timeout | undefined} */
  let settle
  const watcher = fs.watch(root, (_kind, file) => {
    if (file && file !== MAP) return
    clearTimeout(settle)
    settle = setTimeout(reload, 60)
  })

  /**
   * Apply a batch to the file as it is on disk right now, so an edit the AI just saved is never
   * overwritten by the canvas's older copy.
   * @param {Op[]} ops
   */
  function applyToDisk(ops) {
    const fresh = readMap()
    if (fresh.error) return { status: 409, body: { ...snapshot(), error: fresh.error, refused: fresh.error } }
    let map
    try {
      map = applyOps(fresh.map, ops)
    } catch (err) {
      if (!(err instanceof OpError)) throw err
      if (fresh.raw !== lastRaw) { lastRaw = fresh.raw; current = { map: fresh.map, problems: fresh.problems, error: null }; rev++; broadcast('file') }
      return { status: 409, body: { ...snapshot(), refused: err.message } }
    }
    const lines = []
    let at = fresh.map
    for (const op of ops) { lines.push(describeOp(at, op)); at = applyOps(at, [op]) }
    const text = json(map)
    writeAtomic(mapFile, text)
    lastRaw = text
    appendLog(lines)
    current = { map, problems: fresh.problems, error: null }
    rev++
    broadcast('canvas')
    return { status: 200, body: snapshot() }
  }

  /**
   * Only answer pages served from this machine. Requiring a JSON content type on writes also makes
   * any other website's request need a CORS preflight, which this server never grants.
   * @param {http.IncomingMessage} req
   */
  function trusted(req) {
    const host = (req.headers.host ?? '').replace(/:\d+$/, '')
    if (!['localhost', '127.0.0.1', '[::1]'].includes(host)) return false
    if (req.method === 'GET') return true
    return (req.headers['content-type'] ?? '').startsWith('application/json')
  }

  /**
   * Connect-style handler. `next` is called for anything that isn't Dora's API, so Vite can mount it
   * in development; the standalone server passes a static file handler instead.
   * @param {http.IncomingMessage} req @param {http.ServerResponse} res @param {() => void} next
   */
  async function handle(req, res, next) {
    const url = new URL(req.url ?? '/', 'http://localhost')
    if (!url.pathname.startsWith('/api/')) return next()
    if (!trusted(req)) return send(res, 403, { error: 'Dora only answers pages on this machine' })
    try {
      if (req.method === 'GET' && url.pathname === '/api/state') {
        return send(res, 200, { ...snapshot(), layout: readLayout(), workspace: { name, dir: path.resolve(dir) } })
      }
      if (req.method === 'GET' && url.pathname === '/api/events') {
        res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' })
        res.write(`event: state\ndata: ${JSON.stringify({ ...snapshot(), source: 'connect' })}\n\n`)
        clients.add(res)
        const ping = setInterval(() => res.write(': ping\n\n'), 25000)
        req.on('close', () => { clearInterval(ping); clients.delete(res) })
        return
      }
      if (req.method === 'POST' && url.pathname === '/api/ops') {
        const body = await readBody(req)
        if (!Array.isArray(body?.ops)) return send(res, 400, { error: 'Expected { ops: [...] }' })
        const out = applyToDisk(body.ops)
        return send(res, out.status, out.body)
      }
      if (req.method === 'PUT' && url.pathname === '/api/layout') {
        const body = await readBody(req)
        writeAtomic(layoutFile, json(normalizeLayout({ ...body?.layout, version: 2 })))
        return send(res, 200, { ok: true })
      }
      send(res, 404, { error: 'Not found' })
    } catch (err) {
      send(res, 500, { error: err instanceof Error ? err.message : String(err) })
    }
  }

  function close() {
    watcher.close()
    clearTimeout(settle)
    for (const res of clients) res.end()
    clients.clear()
  }

  return { handle, close, root }
}

/** @param {http.ServerResponse} res @param {number} status @param {unknown} body */
function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  res.end(JSON.stringify(body))
}

/** @param {http.IncomingMessage} req @returns {Promise<any>} */
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    /** @type {Buffer[]} */
    const chunks = []
    req.on('data', (c) => {
      size += c.length
      if (size > 5 * 1024 * 1024) { reject(new Error('Request too large')); req.destroy() }
      else chunks.push(c)
    })
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || 'null')) } catch (e) { reject(e) }
    })
    req.on('error', reject)
  })
}

const TYPES = /** @type {Record<string, string>} */ ({
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.png': 'image/png', '.ico': 'image/x-icon', '.json': 'application/json',
})

/** Serve the built canvas from dist/, falling back to index.html. */
function serveStatic(/** @type {http.IncomingMessage} */ req, /** @type {http.ServerResponse} */ res) {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) {
    res.writeHead(503, { 'content-type': 'text/plain; charset=utf-8' })
    return res.end('Dora is not built yet. In the Dora folder, run: npm run build')
  }
  const url = new URL(req.url ?? '/', 'http://localhost')
  let file = path.join(DIST, path.normalize(decodeURIComponent(url.pathname)).replace(/^([/\\])+/, ''))
  if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, 'index.html')
  res.writeHead(200, {
    'content-type': TYPES[path.extname(file)] ?? 'application/octet-stream',
    'cache-control': file.includes(`${path.sep}assets${path.sep}`) ? 'public, max-age=31536000, immutable' : 'no-cache',
  })
  fs.createReadStream(file).pipe(res)
}

/**
 * Start Dora on localhost for `dir`, trying the next port up if one is taken.
 * @param {{ dir: string, port?: number, tries?: number }} opts
 * @returns {Promise<{ url: string, port: number, close: () => Promise<void> }>}
 */
export async function startServer({ dir, port = 4317, tries = 20 }) {
  const dora = createDora({ dir })
  const server = http.createServer((req, res) => { dora.handle(req, res, () => serveStatic(req, res)) })
  for (let p = port; p < port + tries; p++) {
    const ok = await new Promise((resolve, reject) => {
      const onError = (/** @type {NodeJS.ErrnoException} */ err) => {
        server.off('listening', onListening)
        if (err.code === 'EADDRINUSE') resolve(false)
        else reject(err)
      }
      const onListening = () => { server.off('error', onError); resolve(true) }
      server.once('error', onError)
      server.once('listening', onListening)
      server.listen(p, '127.0.0.1')
    })
    if (ok) {
      return {
        url: `http://localhost:${p}`,
        port: p,
        close: () => new Promise((resolve) => { dora.close(); server.close(() => resolve()); server.closeAllConnections?.() }),
      }
    }
  }
  dora.close()
  throw new Error(`Ports ${port}-${port + tries - 1} are all in use. Pass --port to pick another.`)
}
