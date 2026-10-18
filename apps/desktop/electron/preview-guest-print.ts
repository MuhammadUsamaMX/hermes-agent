/**
 * Keep a preview guest's `print()` away from the macOS native print panel
 * (#101880).
 *
 * The desktop app pins Electron 40.10.2 exactly, which predates the fix for
 * electron/electron#50827 (backported to 41-x-y by #51728 and to 42-x-y by
 * #50853, never to 40.x). There, when `UpdatePrinterSettings()` fails,
 * `OnError()` nulls `print_info_` and `AskUserForSettings()` then hands nil to
 * `-[NSPrintPanel runModalWithPrintInfo:]`, which dereferences NULL+0x148 in
 * PrintCore and takes the whole app down with a SIGSEGV. Every bump that would
 * carry the fix is still an open PR, so the app cannot wait for one: the guest
 * must simply never be allowed to build that panel.
 *
 * This stub is installed from the preview guest's preload
 * (preview-guest-preload-entry.ts) into the guest's MAIN world, which is the
 * only seam that runs before page scripts. The guard shipped in #102462 arms on
 * `dom-ready`, and that is too late: a printer-friendly page ending in a bare
 * `<script>window.print();</script>` calls print while the document is still
 * parsing, long before `dom-ready` fires. That is the crash loop @aineko42 hit
 * on launch — a link in a reply was enough, with no user gesture.
 *
 * `contextBridge.executeInMainWorld` serializes the function below and
 * evaluates it inside the guest, so the body has to stay self-contained: it
 * must reference nothing but globals, or it throws ReferenceError there and
 * print is silently left un-stubbed. preview-guest-print.test.ts asserts that
 * shape, and also that the function's text carries no `import`, no `require`,
 * and no reference to the exported message constant.
 */

/**
 * What the guest says in place of printing. The stub embeds this text
 * literally rather than reading the constant (the guest cannot see this
 * module), so the two must stay in step — the suite asserts they match.
 */
export const GUEST_PRINT_BLOCKED_MESSAGE =
  '[Hermes] Printing is disabled in the in-app preview. Open the page in your browser to print.'

export function installGuestPrintStubInMainWorld(): void {
  const guest = globalThis as unknown as {
    __hermesPrintGuard?: boolean
    print?: () => void
  }

  guest.__hermesPrintGuard = true

  guest.print = () => {
    console.warn('[Hermes] Printing is disabled in the in-app preview. Open the page in your browser to print.')
  }
}