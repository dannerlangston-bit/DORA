#!/usr/bin/env node
// @ts-check
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { ensureWorkspace, startServer } from '../server/dora-server.js'

const PKG_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const { version } = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, 'package.json'), 'utf8'))

const HELP = `Dora ${version}: a map that explains a project.

Usage
  dora [folder] [--port 4317] [--open] [--no-skill]

  folder      the project to map (default: the folder you're in)
  --port      where to serve it (default 4317; the next free port if taken)
  --open      also open it in your default browser
  --no-skill  don't add the AI instructions at .claude/skills/dora/SKILL.md

The map is saved in <folder>/.dora/. Commit it with the project.`

/** @param {string[]} argv */
function parse(argv) {
  const out = { dir: '.', port: 4317, open: false, skill: true, help: false, version: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '-h' || a === '--help') out.help = true
    else if (a === '-v' || a === '--version') out.version = true
    else if (a === '--open') out.open = true
    else if (a === '--no-skill') out.skill = false
    else if (a === '--port' || a === '-p') out.port = Number(argv[++i])
    else if (a.startsWith('--port=')) out.port = Number(a.slice(7))
    else if (a.startsWith('-')) { console.error(`Unknown option ${a}\n\n${HELP}`); process.exit(2) }
    else out.dir = a
  }
  if (!Number.isInteger(out.port) || out.port < 1 || out.port > 65535) { console.error('--port needs a number between 1 and 65535'); process.exit(2) }
  return out
}

/**
 * Give the project's AI the instructions for reading and editing the map. Claude Code picks up
 * skills from .claude/skills/. Never overwrites one that's there: the person may have edited it.
 * @param {string} dir
 */
function addSkill(dir) {
  const target = path.join(dir, '.claude', 'skills', 'dora', 'SKILL.md')
  if (fs.existsSync(target)) return null
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.copyFileSync(path.join(PKG_ROOT, 'skill', 'SKILL.md'), target)
  return path.relative(dir, target)
}

/** @param {string} url */
function openBrowser(url) {
  const cmd = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'cmd' : 'xdg-open'
  const args = process.platform === 'win32' ? ['/c', 'start', '', url] : [url]
  spawn(cmd, args, { stdio: 'ignore', detached: true }).on('error', () => {}).unref()
}

async function main() {
  const opts = parse(process.argv.slice(2))
  if (opts.help) { console.log(HELP); return }
  if (opts.version) { console.log(version); return }
  const dir = path.resolve(opts.dir)
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) { console.error(`No folder at ${dir}`); process.exit(1) }

  const { created } = ensureWorkspace(dir)
  const skill = opts.skill ? addSkill(dir) : null
  const server = await startServer({ dir, port: opts.port })

  const inVsCode = process.env.TERM_PROGRAM === 'vscode'
  const lines = [
    '',
    `  Dora is mapping ${dir}`,
    '',
    `  Map     ${server.url}`,
    `  File    .dora/map.json  (you and your AI both edit this)`,
  ]
  if (created.length) lines.push(`  Made    ${created.join(', ')}`)
  if (skill) lines.push(`  AI      ${skill}  (tells your AI how to read and fill the map)`)
  lines.push('')
  lines.push(inVsCode
    ? `  Open it in a VS Code tab: Cmd/Ctrl+Shift+P, "Simple Browser: Show", paste ${server.url}`
    : `  Open ${server.url} in a browser, or in VS Code's Simple Browser.`)
  lines.push('  Ctrl+C stops Dora. The map stays in .dora/.', '')
  console.log(lines.join('\n'))
  if (opts.open) openBrowser(server.url)

  const stop = () => { void server.close().then(() => process.exit(0)) }
  process.on('SIGINT', stop)
  process.on('SIGTERM', stop)
}

main().catch((err) => { console.error(err instanceof Error ? err.message : err); process.exit(1) })
