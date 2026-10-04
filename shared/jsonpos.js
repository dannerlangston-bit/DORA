// @ts-check
/**
 * Where a JSON text first goes wrong, as a line number. JSON.parse's messages only sometimes say,
 * and "line 12" is what someone fixing the file by hand (or an AI) needs.
 * @param {string} text @returns {number | null} 1-based line, or null if the text parses
 */
export function jsonErrorLine(text) {
  let i = 0
  const ws = () => { while (i < text.length && ' \t\n\r'.includes(text[i])) i++ }
  const fail = () => { throw i }
  const lit = (/** @type {string} */ word) => { if (text.startsWith(word, i)) i += word.length; else fail() }
  const string = () => {
    i++
    while (i < text.length && text[i] !== '"') {
      if (text[i] === '\\') i++
      else if (text.charCodeAt(i) < 0x20) fail()
      i++
    }
    if (text[i] !== '"') fail()
    i++
  }
  const number = () => {
    const m = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i))
    if (!m) fail()
    i += /** @type {RegExpExecArray} */ (m)[0].length
  }
  /** @returns {void} */
  const value = () => {
    ws()
    const c = text[i]
    if (c === '{') {
      i++; ws()
      if (text[i] === '}') { i++; return }
      for (;;) {
        ws(); if (text[i] !== '"') fail(); string(); ws()
        if (text[i] !== ':') fail(); i++; value(); ws()
        if (text[i] === ',') { i++; continue }
        if (text[i] === '}') { i++; return }
        fail()
      }
    }
    if (c === '[') {
      i++; ws()
      if (text[i] === ']') { i++; return }
      for (;;) {
        value(); ws()
        if (text[i] === ',') { i++; continue }
        if (text[i] === ']') { i++; return }
        fail()
      }
    }
    if (c === '"') return string()
    if (c === 't') return lit('true')
    if (c === 'f') return lit('false')
    if (c === 'n') return lit('null')
    return number()
  }
  try {
    value(); ws()
    if (i < text.length) fail()
    return null
  } catch (at) {
    if (typeof at !== 'number') throw at
    return text.slice(0, at).split('\n').length
  }
}
