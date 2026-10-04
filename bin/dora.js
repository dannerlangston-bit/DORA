#!/usr/bin/env node
// @ts-check
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { checkMap } from '../shared/check.js'
import { ensureWorkspace, startServer } from '../server/dora-server.js'

const PKG_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const { version } = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, 'package.json'), 'utf8'))

const HELP = `Dora ${version}: a map that explains a project.

Usage
  dora [folder] [--port 4317] [--open] [--no-skill] [--update-skill]
  dora check [folder]

  folder          the project to map (default: the folder you're in)
  --port          where to serve it (default 4317; the next free port if taken)
  --open          also open it in your default browser
  --no-skill      don't add the AI instructions at .claude/skills/dora/SKILL.md
  --update-skill  replace that file with this version of Dora's instructions

  check           list what's wrong with .dora/map.json and exit (1 if anything
                  must be fixed). Your AI runs this after editing the map.

The map is saved in <folder>/.dora/. Commit it with the project.`

/** @param {string[]} argv */
function parse(argv) {
  const out = { command: 'serve', dir: '.', port: 4317, open: false, skill: true, updateSkill: false, help: false, version: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (i === 0 && a === 'check') out.command = 'check'
    else if (a === '-h' || a === '--help') out.help = true
    else if (a === '--update-skill') out.updateSkill = true
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

const SKILL_SOURCE = path.join(PKG_ROOT, 'skill', 'SKILL.md')
/** @param {string} text */
const skillVersion = (text) => Number(/dora-skill-version: (\d+)/.exec(text)?.[1] ?? 0)

/**
 * Give the project's AI the instructions for reading and editing the map. Claude Code picks up
 * skills from .claude/skills/. Never overwrites one that's there unless asked (the person may have
 * edited it); says so when this Dora has newer instructions.
 * @param {string} dir @param {boolean} update
 * @returns {{ path: string, state: 'added' | 'updated' | 'current' | 'outdated' }}
 */
function syncSkill(dir, update) {
  const target = path.join(dir, '.claude', 'skills', 'dora', 'SKILL.md')
  const rel = path.relative(dir, target)
  const source = fs.readFileSync(SKILL_SOURCE, 'utf8')
  if (!fs.existsSync(target)) {
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, source)
    return { path: rel, state: 'added' }
  }
  const have = fs.readFileSync(target, 'utf8')
  if (have === source) return { path: rel, state: 'current' }
  if (update) { fs.writeFileSync(target, source); return { path: rel, state: 'updated' } }
  return { path: rel, state: skillVersion(have) < skillVersion(source) ? 'outdated' : 'current' }
}

/** `dora check`: print what's wrong with the map, exit 1 if anything must be fixed. @param {string} dir */
function check(dir) {
  const file = path.join(dir, '.dora', 'map.json')
  if (!fs.existsSync(file)) { console.error(`No map at ${file}. Run dora in that folder first.`); process.exit(1) }
  const { errors, warnings, stats } = checkMap(fs.readFileSync(file, 'utf8'))
  const shown = path.relative(process.cwd(), file)
  console.log(`Dora check: ${shown.startsWith('..') ? file : shown}`)
  console.log(`  ${stats.events} events, ${stats.links} links, ${stats.workflows} workflows`)
  if (errors.length) console.log(`\n  Must fix (${errors.length})\n${errors.map((e) => `  - ${e}`).join('\n')}`)
  if (warnings.length) console.log(`\n  Worth fixing (${warnings.length})\n${warnings.map((w) => `  - ${w}`).join('\n')}`)
  if (!errors.length && !warnings.length) console.log('\n  All good.')
  process.exit(errors.length ? 1 : 0)
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

  if (opts.command === 'check') return check(dir)

  const { created } = ensureWorkspace(dir)
  const skill = opts.skill || opts.updateSkill ? syncSkill(dir, opts.updateSkill) : null
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
  if (skill?.state === 'added') lines.push(`  AI      ${skill.path}  (tells your AI how to read and fill the map)`)
  if (skill?.state === 'updated') lines.push(`  AI      ${skill.path} updated to this version's instructions`)
  if (skill?.state === 'outdated') lines.push(`  AI      ${skill.path} is older than this Dora. Run: dora --update-skill`)
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
