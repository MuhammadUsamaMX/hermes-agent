import { describe, expect, it } from 'vitest'

import { hasClosePreviewFlag, previewGuestInputAction } from './preview-guest-escape'

describe('previewGuestInputAction', () => {
  it('exits fullscreen on Escape only while the host window is fullscreen', () => {
    expect(previewGuestInputAction({ key: 'Escape', type: 'keyDown' }, true)).toBe('exit-fullscreen')
    // A pane in normal mode keeps passing Esc through to the page.
    expect(previewGuestInputAction({ key: 'Escape', type: 'keyDown' }, false)).toBeNull()
  })

  it('closes the preview on Ctrl+Shift+W regardless of fullscreen state', () => {
    expect(previewGuestInputAction({ control: true, shift: true, key: 'w', type: 'keyDown' }, true)).toBe(
      'close-preview'
    )
    expect(previewGuestInputAction({ control: true, shift: true, key: 'w', type: 'keyDown' }, false)).toBe(
      'close-preview'
    )
  })

  it('also accepts the Cmd+Shift+W (macOS) form', () => {
    expect(previewGuestInputAction({ meta: true, shift: true, key: 'W', type: 'keyDown' }, true)).toBe('close-preview')
  })

  // #101880: ⌘P in a guest builds the same native print panel that segfaults,
  // and it never passes through the page's own `print()`, so no in-page stub
  // can catch it. `before-input-event` is the only seam that sees it first.
  it('blocks the print chord in both the Ctrl and Cmd forms', () => {
    expect(previewGuestInputAction({ control: true, key: 'p', type: 'keyDown' }, false)).toBe('block-print')
    expect(previewGuestInputAction({ meta: true, key: 'P', type: 'keyDown' }, true)).toBe('block-print')
  })

  it('leaves typing P and the other print modifiers to the page', () => {
    // Plain P is typing.
    expect(previewGuestInputAction({ key: 'p', type: 'keyDown' }, false)).toBeNull()
    // ⌘⇧P and friends are not the print chord.
    expect(previewGuestInputAction({ meta: true, shift: true, key: 'p', type: 'keyDown' }, false)).toBeNull()
    expect(previewGuestInputAction({ control: true, key: 'p', type: 'keyUp' }, false)).toBeNull()
  })

  it('leaves every other key for the page', () => {
    expect(previewGuestInputAction({ key: 'a', type: 'keyDown' }, true)).toBeNull()
    // Bare W / Ctrl+W without Shift is not the close chord.
    expect(previewGuestInputAction({ control: true, key: 'w', type: 'keyDown' }, true)).toBeNull()
    // Shift+W without the modifier is typing, not closing.
    expect(previewGuestInputAction({ shift: true, key: 'W', type: 'keyDown' }, true)).toBeNull()
  })

  it('ignores key releases and auto-repeats', () => {
    expect(previewGuestInputAction({ key: 'Escape', type: 'keyUp' }, true)).toBeNull()
    expect(previewGuestInputAction({ isAutoRepeat: true, key: 'Escape', type: 'keyDown' }, true)).toBeNull()
  })
})

describe('hasClosePreviewFlag', () => {
  it('detects the flag in both forms', () => {
    expect(hasClosePreviewFlag(['Hermes', '--close-preview'])).toBe(true)
    expect(hasClosePreviewFlag(['Hermes', '--close-preview=1'])).toBe(true)
  })

  it('ignores other argv and empty argv', () => {
    expect(hasClosePreviewFlag(['Hermes'])).toBe(false)
    expect(hasClosePreviewFlag(['--close'])).toBe(false)
    expect(hasClosePreviewFlag([])).toBe(false)
  })
})
