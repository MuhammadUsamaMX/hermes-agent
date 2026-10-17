/**
 * Publish a bundle atomically so a concurrent reader never loads a partial file.
 *
 * Issue #121286: `node dist/entry.js` is exec'd while other launches may still be
 * rebuilding that same file. Writing the bundle in place exposes a truncated
 * bundle to the reader — a `SyntaxError` on startup, surfacing in the dashboard
 * as the Chat tab's PTY dying during connect.
 *
 * We write a uniquely-named sibling temp file and `rename()` it over the target.
 * rename() is atomic within a filesystem, so a reader sees either the previous
 * bundle or the new one — never a half-written mix.
 */

import { mkdirSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'

const RENAME_ATTEMPTS = 40
const RENAME_BACKOFF_MS = 25

let tempCounter = 0

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** Windows/AV refusals to replace a file another process still holds open. */
function isTransientRenameError(error) {
  return error.code === 'EPERM' || error.code === 'EACCES' || error.code === 'EBUSY'
}

export async function publishBundle(targetPath, contents) {
  const dir = dirname(targetPath)

  mkdirSync(dir, { recursive: true })

  // Sibling temp path: same directory means same filesystem, which is what makes
  // the rename atomic. The pid + counter suffix keeps two concurrent builds from
  // sharing a temp path and corrupting each other's output.
  tempCounter += 1

  const tempPath = join(dir, `.${basename(targetPath)}.${process.pid}.${tempCounter}.tmp`)

  writeFileSync(tempPath, contents)

  for (let attempt = 1; ; attempt += 1) {
    try {
      renameSync(tempPath, targetPath)

      return
    } catch (error) {
      if (attempt < RENAME_ATTEMPTS && isTransientRenameError(error)) {
        await sleep(RENAME_BACKOFF_MS)
        continue
      }

      // The rename can still be refused after the retries (a long-lived Windows
      // file lock). Falling back to a direct write keeps the launch working,
      // exactly as it did before this fix; only that rare locked-file case is
      // non-atomic.
      rmSync(tempPath, { force: true })
      writeFileSync(targetPath, contents)

      return
    }
  }
}