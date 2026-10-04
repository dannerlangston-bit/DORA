// @ts-check
// Dora on a throwaway project folder, for the acceptance tests.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { startServer } from '../server/dora-server.js'

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '.work')
fs.rmSync(dir, { recursive: true, force: true })
fs.mkdirSync(dir, { recursive: true })
await startServer({ dir, port: 4411, tries: 1 })
