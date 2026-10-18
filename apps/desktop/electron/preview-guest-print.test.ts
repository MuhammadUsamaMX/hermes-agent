import { afterEach, describe, expect, it } from 'vitest'

import { GUEST_PRINT_BLOCKED_MESSAGE, installGuestPrintStubInMainWorld } from './preview-guest-print'

// #101880: a preview guest's `print()` reaches the macOS native print panel,
// whose construction segfaults inside PrintCore and takes the whole app down.
//
// The stub is installed from the preload into the guest's MAIN world BEFORE
// any page script runs. That timing is the whole fix: a bare inline
// `<script>window.print()</script>` executes while the document is still
// parsing, long before `dom-ready` fires, so a dom-ready guard never sees it.
interface GuestGlobal {
  __hermesPrintGuard?: boolean
  print?: () => unknown
}

const guestGlobal = globalThis as unknown as GuestGlobal

// Stands in for the native binding: if the stub is not installed, calling
// print() reaches it and the test fails exactly the way the app crashes.
const nativePrint = () => {
  throw new Error('native print panel reached')
}

afterEach(() => {
  delete guestGlobal.print
  delete guestGlobal.__hermesPrintGuard
})

function captureWarnings(run: () => void): string[] {
  const warnings: string[] = []
  const original = console.warn

  console.warn = (message: string) => {
    warnings.push(message)
  }

  try {
    run()
  } finally {
    console.warn = original
  }

  return warnings
}

describe('installGuestPrintStubInMainWorld', () => {
  it('replaces the guest print so the native panel is never built', () => {
    guestGlobal.print = nativePrint

    const warnings = captureWarnings(() => {
      installGuestPrintStubInMainWorld()

      expect(() => guestGlobal.print?.()).not.toThrow()
    })

    expect(warnings).toContain(GUEST_PRINT_BLOCKED_MESSAGE)
  })

  it('marks the document so re-arms stay idempotent', () => {
    guestGlobal.print = nativePrint

    installGuestPrintStubInMainWorld()
    installGuestPrintStubInMainWorld()

    expect(guestGlobal.__hermesPrintGuard).toBe(true)
    expect(() => guestGlobal.print?.()).not.toThrow()
  })

  it('re-stubs a print the page replaced after load', () => {
    guestGlobal.print = nativePrint

    installGuestPrintStubInMainWorld()
    // A guest that reassigns print (SPA router, lazy widget) is re-stubbed on
    // the next arm, so the native panel stays unreachable.
    guestGlobal.print = nativePrint

    installGuestPrintStubInMainWorld()

    expect(() => guestGlobal.print?.()).not.toThrow()
  })

  it('warns with exactly the message the module exports', () => {
    // The stub body embeds the text literally, because the guest cannot see
    // this module. This is the guard against those two copies drifting.
    guestGlobal.print = nativePrint

    installGuestPrintStubInMainWorld()

    const warnings = captureWarnings(() => guestGlobal.print?.())

    expect(warnings).toEqual([GUEST_PRINT_BLOCKED_MESSAGE])
  })

  it('is self-contained: the function body closes over nothing it cannot see in the guest', () => {
    // executeInMainWorld serializes this function and evaluates it inside the
    // guest. Any reference to a module-scope import would throw ReferenceError
    // there, silently un-stubbing print. Assert the shape that survives
    // serialization: a single self-contained function declaration.
    const source = installGuestPrintStubInMainWorld.toString()

    expect(source).not.toMatch(/\bimport\b|\brequire\(/)
    expect(source).not.toMatch(/\bGUEST_PRINT_BLOCKED_MESSAGE\b/)
  })
})