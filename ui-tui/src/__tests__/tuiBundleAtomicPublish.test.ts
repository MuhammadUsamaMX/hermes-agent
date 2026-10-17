/**
 * Atomic bundle publish — regression for issue #121286.
 *
 * Every `hermes --tui` launch, and every dashboard Chat-tab PTY, runs the
 * esbuild bundle build before exec'ing `node dist/entry.js`. When that build
 * rewrites `dist/entry.js` in place, a launch that starts while another launch
 * is still writing loads a TRUNCATED bundle and dies immediately with a
 * `SyntaxError` (in the dashboard: the Chat tab's PTY closes during connect).
 *
 * The invariant pinned here: publishing the bundle is atomic. A reader sampling
 * the bundle while N builds run in parallel only ever observes a COMPLETE
 * bundle — never a partial one.
 *
 * The build test drives the real `scripts/build.mjs` against a generated
 * fixture project, so it fails on the pre-fix in-place write and passes once
 * the build publishes through a temp file + rename(). It skips when esbuild
 * cannot be resolved (partial install).
 */

import { spawn } from 'node:child_process'
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterAll, describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))

const uiTuiRoot = resolve(here, '..', '..')

const publishBundlePath = resolve(uiTuiRoot, 'scripts', 'publishBundle.mjs')
const publishBundleUrl = new URL(`file://${publishBundlePath}`).href

const PAYLOAD_BYTES = 4 * 1024 * 1024
const WRITERS = 4
const ROUNDS = 3
const BUILD_ROUNDS = 4

const scratchDirs: string[] = []

afterAll(() => {
  for (const dir of scratchDirs) {
    rmSync(dir, { recursive: true, force: true })
  }
})

function makeScratch(): string {
  const dir = mkdtempSync(join(tmpdir(), 'tui-bundle-publish-'))
  scratchDirs.push(dir)

  return dir
}

/** Yield to the event loop so child-process exit callbacks can run. */
const tick = () => new Promise((done) => { setImmediate(done) })

function esbuildAvailable(): boolean {
  return existsSync(resolve(uiTuiRoot, 'node_modules', 'esbuild', 'package.json'))
}

/**
 * Spawn `node <args>` in *cwd*; resolve on exit 0, reject with captured stderr.
 * The returned promise plus the shared counter let the caller sample the bundle
 * while the children are still running.
 */
function runNode(
  args: string[],
  cwd: string | undefined,
  onDone: () => void
): Promise<void> {
  return new Promise<void>((done, fail) => {
    const child = spawn(process.execPath, args, { cwd, stdio: ['ignore', 'ignore', 'pipe'] })

    let stderr = ''
    child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.on('error', fail)
    child.on('exit', (code) => {
      if (code !== 0) {
        fail(new Error(`${args.join(' ')} exited ${code}: ${stderr}`))

        return
      }

      onDone()
      done()
    })
  })
}

describe('scripts/build.mjs atomic publish', () => {
  it('never publishes a partial dist/entry.js when builds run concurrently', async () => {
    if (!esbuildAvailable()) {
      // Partial install: esbuild is unresolvable, so there is nothing to assert.
      return
    }

    // Fixture project outside the repo, with node_modules symlinked so the real
    // build script resolves the workspace's esbuild.
    const fixtureRoot = makeScratch()

    mkdirSync(resolve(fixtureRoot, 'scripts'), { recursive: true })
    mkdirSync(resolve(fixtureRoot, 'src'), { recursive: true })
    symlinkSync(resolve(uiTuiRoot, 'node_modules'), resolve(fixtureRoot, 'node_modules'), 'junction')

    copyFileSync(resolve(uiTuiRoot, 'scripts/build.mjs'), resolve(fixtureRoot, 'scripts/build.mjs'))

    // publishBundle.mjs is what build.mjs publishes through; before the fix it
    // does not exist, and the pre-fix build.mjs does not import it.
    if (existsSync(publishBundlePath)) {
      copyFileSync(publishBundlePath, resolve(fixtureRoot, 'scripts/publishBundle.mjs'))
    }

    // A source big enough that a single in-place write is observable as a
    // partial read, so the pre-fix behaviour actually fails this test.
    const lines = ['// fixture entry']
    for (let i = 0; i < 40_000; i += 1) {
      lines.push(`export const v${i} = ${i} + 'padding-padding-padding-${i}';`)
    }
    lines.push("console.log('FIXTURE_END_MARKER');")

    writeFileSync(resolve(fixtureRoot, 'src/entry.tsx'), `${lines.join('\n')}\n`)

    const target = resolve(fixtureRoot, 'dist/entry.js')

    const partial: string[] = []
    let reads = 0

    // Several rounds of concurrent builds: a single in-place write is only
    // exposed for a few ms, so one round can miss the race by luck. Repeated
    // rounds make the pre-fix in-place write fail reliably.
    for (let round = 0; round < BUILD_ROUNDS; round += 1) {
      let finished = 0
      const builders = Array.from({ length: 3 }, () => runNode(['scripts/build.mjs'], fixtureRoot, () => {
        finished += 1
      }))

      while (finished < builders.length) {
        try {
          const body = readFileSync(target, 'utf8')
          reads += 1

          if (!body.includes('FIXTURE_END_MARKER')) {
            partial.push(`${body.length} bytes`)
          }
        } catch (error) {
          if (error.code !== 'ENOENT') { throw error }
        }

        await tick()
      }

      await Promise.all(builders)
    }

    expect(readFileSync(target, 'utf8')).toContain('FIXTURE_END_MARKER')
    expect(reads, 'reader never sampled the bundle — the race is not being exercised').toBeGreaterThan(0)
    expect(partial, `concurrent builds exposed a partial bundle:\n${partial.slice(0, 5).join('\n')}`).toEqual([])
  }, 180_000)
})

describe('publishBundle', () => {
  it('publishes the exact bundle contents to the target path', async () => {
    const { publishBundle } = await import(publishBundleUrl)

    const target = resolve(makeScratch(), 'nested', 'entry.js')

    await publishBundle(target, 'console.log("bundle")\n')

    expect(readFileSync(target, 'utf8')).toBe('console.log("bundle")\n')
  })

  it('leaves no temp files behind after publishing', async () => {
    const { publishBundle } = await import(publishBundleUrl)

    const dir = makeScratch()

    await publishBundle(resolve(dir, 'entry.js'), 'bundle\n')

    expect(readdirSync(dir)).toEqual(['entry.js'])
  })

  it('never exposes a partial bundle to a concurrent reader', async () => {
    const dir = makeScratch()
    const target = resolve(dir, 'entry.js')

    // Each writer publishes its own large, distinct bundle: a reader that ever
    // catches a half-written file sees a payload no writer owns.
    const payloads = Array.from({ length: WRITERS }, (_, id) => {
      const head = `/*START-${id}*/\n`
      const tail = `\n/*END-${id}*/\n`

      return head + 'x'.repeat(PAYLOAD_BYTES) + tail
    })

    const complete = new Set(payloads)

    const writerSource = `
import { readFileSync } from 'node:fs'
import { publishBundle } from ${JSON.stringify(publishBundleUrl)}

const [target, payloadPath] = process.argv.slice(1)
const payload = readFileSync(payloadPath, 'utf8')

for (let round = 0; round < ${ROUNDS}; round += 1) {
  await publishBundle(target, payload)
}
`

    let finished = 0

    const writers = Array.from({ length: WRITERS }, (_, id) => {
      const payloadPath = resolve(dir, `payload-${id}.js`)
      writeFileSync(payloadPath, payloads[id])

      return runNode(['--input-type=module', '-e', writerSource, target, payloadPath], undefined, () => {
        finished += 1
      })
    })

    const partial: string[] = []
    let reads = 0

    while (finished < WRITERS) {
      try {
        const body = readFileSync(target, 'utf8')
        reads += 1

        if (!complete.has(body)) {
          partial.push(`${body.length} bytes, starts ${JSON.stringify(body.slice(0, 24))}`)
        }
      } catch (error) {
        if (error.code !== 'ENOENT') { throw error }
      }

      await tick()
    }

    await Promise.all(writers)

    expect(reads, 'reader never sampled the bundle — the race is not being exercised').toBeGreaterThan(0)
    expect(partial, `readers observed partial bundles:\n${partial.slice(0, 5).join('\n')}`).toEqual([])
  }, 120_000)
})