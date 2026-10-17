#!/usr/bin/env node
// Bundles src/entry.tsx into a single self-contained dist/entry.js.
// No runtime node_modules needed.
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import { publishBundle } from './publishBundle.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const out = resolve(root, 'dist/entry.js')

// `react-devtools-core` is only imported when DEV=true at runtime (Ink dev
// mode). Stub it out so the bundle doesn't carry the dep.
const stubDevtools = {
  name: 'stub-react-devtools-core',
  setup(b) {
    b.onResolve({ filter: /^react-devtools-core$/ }, args => ({
      path: args.path,
      namespace: 'stub-devtools'
    }))
    b.onLoad({ filter: /.*/, namespace: 'stub-devtools' }, () => ({
      contents: 'export default { initialize() {}, connectToDevTools() {} }',
      loader: 'js'
    }))
  }
}

const result = await build({
  entryPoints: [resolve(root, 'src/entry.tsx')],
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node20',
  outfile: out,
  jsx: 'automatic',
  jsxImportSource: 'react',
  // Skip the prebuilt @hermes/ink bundle and inline the source instead:
  // (1) esbuild's `__esm` helper does not await nested async init, so the
  //     prebuilt bundle's lazy `render` would never resolve when nested in
  //     this top-level Promise.all; (2) bundling from source also lets us
  //     keep `ink-text-input` and the upstream `ink` graph OUT of the
  //     bundle entirely — re-exporting them from entry-exports created a
  //     circular async chain that hung the TUI at startup with only ANSI
  //     reset bytes on screen (#31227).
  alias: { '@hermes/ink': resolve(root, 'packages/hermes-ink/src/entry-exports.ts') },
  plugins: [stubDevtools],
  // Some transitive deps use CommonJS `require(...)` at runtime. ESM bundles
  // don't get a `require` binding automatically, so we inject one.
  banner: {
    js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);"
  },
  // Keep the bundle in memory instead of letting esbuild write dist/entry.js
  // directly: publishBundle() below puts it in place with a temp file + rename.
  // Concurrent launches (two dashboards, a dashboard plus `hermes --tui`, or
  // parallel test workers) each rebuild this same path, and an in-place write
  // let a TUI load a truncated bundle and die with a SyntaxError (#121286).
  write: false,
  logLevel: 'info'
})

// esbuild preserves the shebang from src/entry.tsx into the bundle, but Nix's
// patchShebangs phase mangles `/usr/bin/env -S node --foo --bar` (it strips
// the `node` token, leaving a broken interpreter). The hermes_cli launcher
// always invokes this file as `node dist/entry.js` anyway, so the shebang is
// redundant — strip it.
const bundle = result.outputFiles[0].text
const body = bundle.startsWith('#!') ? bundle.slice(bundle.indexOf('\n') + 1) : bundle

// Write via temp file + rename so a concurrent `node dist/entry.js` reads either
// the previous bundle or this one, never a half-written file (#121286).
await publishBundle(out, body)

console.log(`built ${out}`)
