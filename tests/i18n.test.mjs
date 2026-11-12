import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const SOURCE = fs.readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
const PLUGIN = 'sheets'

// The bundles and the function that turns them into the tree Hermes is given.
const { MESSAGES, messageTree } = vm.runInNewContext(`${SOURCE.slice(SOURCE.indexOf('const MESSAGES = {'), SOURCE.indexOf('/** Module-level translator'))}\n;({ MESSAGES, messageTree })`)

/** The lookup Hermes does (apps/desktop/src/i18n/runtime.ts): walk the key as a dot-path into
 *  the registered tree, the active language first, then English, then the key itself. */
function translate(locale, key, ...args) {
  const walk = tree => key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), tree)

  for (const flat of [MESSAGES[locale], MESSAGES.en]) {
    const value = flat ? walk(messageTree(flat)) : undefined

    if (typeof value === 'string') {
      return value
    }

    if (typeof value === 'function') {
      return value(...args)
    }
  }

  return key
}

test('English and Chinese have the same keys', () => {
  assert.deepEqual(Object.keys(MESSAGES.zh).sort(), Object.keys(MESSAGES.en).sort())
  assert.equal(Object.keys(MESSAGES.en).length > 30, true)
})

test('every key resolves through the nested lookup Hermes uses, in both languages', () => {
  for (const locale of ['en', 'zh']) {
    for (const key of Object.keys(MESSAGES.en)) {
      // A key that comes back as itself was not found in the tree.
      assert.notEqual(translate(locale, key, 3), key, `${locale}: ${key}`)
    }
  }

  assert.equal(translate('zh', 'bar.runAll', 3), '全部运行')
  // A language with no bundle falls back to English, and an unknown key to itself.
  assert.equal(translate('de', 'nav'), MESSAGES.en.nav)
  assert.equal(translate('zh', 'no.such.key'), 'no.such.key')
})

test('every key the page asks for is in the bundles', () => {
  const asked = [...SOURCE.matchAll(/(?<![\w.$])tr?\('([\w.]+)'/g)].map(match => match[1])

  assert.equal(asked.length > 30, true)
  assert.deepEqual([...new Set(asked)].filter(key => !(key in MESSAGES.en)), [])
})

test('every component that looks up copy asks for the translator first', () => {
  const lines = SOURCE.split('\n')
  const starts = lines.flatMap((line, index) => (/^(async )?function \w+/.test(line) ? [index] : []))
  const missing = []

  starts.forEach((start, at) => {
    const body = lines.slice(start, starts[at + 1] ?? lines.length).join('\n')

    // A component that calls t() without it throws "t is not defined" when the page renders.
    if (/(?<![\w.$])t\(['`a-zA-Z]/.test(body) && !body.includes(`const t = usePluginI18n('${PLUGIN}')`)) {
      missing.push(lines[start].slice(0, 50))
    }
  })

  assert.deepEqual(missing, [])
})

test('the bundles are registered as trees, and the labels are rebuilt when the language changes', () => {
  assert.equal(SOURCE.includes('ctx.i18n.register(Object.fromEntries(Object.entries(MESSAGES).map(([locale, flat]) => [locale, messageTree(flat)])))'), true)
  assert.equal(SOURCE.includes('ctx.i18n?.onLocaleChange?.('), true)
  assert.equal(SOURCE.includes("label: tr('nav')"), true)
})
