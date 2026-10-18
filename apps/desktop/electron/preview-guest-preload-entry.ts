// Preload for the preview pane's `<webview>` guests. main.ts installs this
// file via `will-attach-webview` on the `persist:hermes-preview` partition
// only (see `installPreviewGuestPreload`), so no other webview inherits it.
//
// The guest runs with contextIsolation, so this preload shares the guest's
// DOM but never its JavaScript world. A preview page's `target="_blank"`
// anchors (Streamlit traceback's "Ask Google" / "Ask …" buttons —
// #112941) are intercepted here in the DOM's capture phase and handed to the
// host renderer via `sendToHost`; the host admits the scheme and routes the
// URL through the audited `hermes:openExternal` channel. A guest URL never
// becomes an Electron popup and this side never opens anything by itself.
//
// Deliberate scope: only trusted anchor clicks are forwarded. A page's direct
// `window.open` calls stay blocked (the webview has no `allowpopups`): hooking
// them would mean reaching into the guest's JS world, and this preload exposes
// nothing there.

import { installGuestExternalHandoff } from './preview-guest-preload'
import { installGuestPrintStubInMainWorld } from './preview-guest-print'

const electron = require('electron') as {
  contextBridge: { executeInMainWorld(executionScript: unknown): unknown }
  ipcRenderer: { sendToHost(channel: string, ...args: unknown[]): void }
}

// #101880: stub `print` in the guest's MAIN world now, while this preload runs
// — before the document is parsed and therefore before any page script. The
// renderer's own guard arms on `dom-ready`, which is too late for a page that
// ends in a bare `<script>window.print();</script>`; that page called print
// while parsing and took the app down before the dialog ever appeared.
electron.contextBridge.executeInMainWorld(installGuestPrintStubInMainWorld)

installGuestExternalHandoff({
  addEventListener: (type, listener, capture) => document.addEventListener(type, listener, capture),
  sendToHost: (channel, ...args) => electron.ipcRenderer.sendToHost(channel, ...args)
})
