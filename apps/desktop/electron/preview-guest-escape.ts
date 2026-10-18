/**
 * The preview pane's escape hatch (#97213).
 *
 * A `<webview>` guest is its own out-of-process webContents: once a guest
 * page takes HTML5 fullscreen it owns the input focus, and nothing in the
 * host renderer — menus, HUD chords, the app's own keybinds — can reach
 * the user while the guest holds the screen. On Wayland there is no
 * xdotool/wmctrl to force a window change from outside either, so a page
 * that swallows Esc (or simply never offers one) locks the whole display.
 *
 * Electron's `before-input-event` fires on the guest's webContents BEFORE
 * the page sees the key, which is the one interception point the host
 * always owns. This module holds the pure routing decision so the policy
 * is unit-testable without a live guest.
 */

export type PreviewGuestInputAction = 'close-preview' | 'exit-fullscreen' | 'block-print'

/** The subset of Electron's `Input` the decision needs. */
export interface PreviewGuestInput {
  control?: boolean
  isAutoRepeat?: boolean
  key: string
  meta?: boolean
  shift?: boolean
  type: string
}

/**
 * Decide what a keypress inside a preview guest must trigger on the host.
 *
 * - Escape while the host window is fullscreen: exit fullscreen. Gated on
 *   the fullscreen state so a pane in normal mode keeps passing Esc through
 *   to the page (video players and web UIs use it for their own exits).
 * - Ctrl/Cmd+Shift+W: close the preview pane. This mirrors the host's
 *   close-tab chord so muscle memory works no matter who has focus.
 * - Ctrl/Cmd+P: block the print chord (#101880). It builds the same native
 *   print panel that segfaults inside PrintCore, and unlike a page's own
 *   `print()` call it never passes through page script, so the guest's `print`
 *   stub cannot catch it — this is the only seam that sees it first.
 *   Shift-modified forms (Cmd+Shift+P) are somebody else's chord and pass.
 *
 * Returns null when the page should see the key unchanged.
 */
export function previewGuestInputAction(
  input: PreviewGuestInput,
  hostIsFullscreen: boolean
): PreviewGuestInputAction | null {
  if (input.type !== 'keyDown' || input.isAutoRepeat) {
    return null
  }

  if (input.key === 'Escape') {
    return hostIsFullscreen ? 'exit-fullscreen' : null
  }

  if ((input.control || input.meta) && input.shift && input.key.toLowerCase() === 'w') {
    return 'close-preview'
  }

  // Plain P is typing; only the modifier chord is the print command. Shift is
  // excluded so Cmd+Shift+P (and friends) still belong to the page.
  if ((input.control || input.meta) && !input.shift && input.key.toLowerCase() === 'p') {
    return 'block-print'
  }

  return null
}

/**
 * Recognize the `--close-preview` summon flag in an argv, accepting both
 * the bare form and the `--close-preview=<value>` form Electron may pass
 * through verbatim. Mirrors `hasQuickEntryFlag` so the CLI can forward the
 * same flag shape (#82654's compositor-keybind path).
 */
export function hasClosePreviewFlag(argv: readonly string[]): boolean {
  return argv.some(arg => arg === '--close-preview' || arg.startsWith('--close-preview='))
}
