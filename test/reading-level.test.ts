import fs from 'node:fs'
import { describe, expect, it } from 'vitest'
import { TOUR } from '../src/tourSteps'

/** Rough syllable count: vowel groups, minus a silent final e, at least one. */
function syllables(word: string): number {
  const w = word.toLowerCase().replace(/[^a-z]/g, '')
  if (!w) return 0
  if (w.length <= 3) return 1
  const groups = w.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, '').replace(/^y/, '').match(/[aeiouy]{1,2}/g)
  return Math.max(1, groups?.length ?? 1)
}

/** Flesch-Kincaid grade level. */
export function grade(text: string): number {
  const sentences = text.split(/[.!?]+/).filter((s) => s.trim()).length
  const words = text.split(/\s+/).filter((w) => /[a-z]/i.test(w))
  const syl = words.reduce((n, w) => n + syllables(w), 0)
  return 0.39 * (words.length / sentences) + 11.8 * (syl / words.length) - 15.59
}

describe('the tour reads at about a 5th-grade level', () => {
  it('as a whole', () => {
    const all = TOUR.map((s) => s.body).join(' ')
    expect(grade(all)).toBeLessThanOrEqual(5.5)
  })
  it.each(TOUR.map((s) => [s.title, s.body]))('%s', (_title, body) => {
    expect(grade(body)).toBeLessThanOrEqual(6.5)
  })
})

describe('docs/GUIDE.md reads at about a 5th-grade level', () => {
  it('its sentences (not headings, tables or code)', () => {
    const md = fs.readFileSync(new URL('../docs/GUIDE.md', import.meta.url), 'utf8')
      .replace(/```[\s\S]*?```/g, '')
      .split('\n').filter((l) => !l.startsWith('#') && !l.startsWith('|')).join('\n')
      .replace(/[*`]/g, '')
    expect(grade(md)).toBeLessThanOrEqual(5.5)
  })
})
