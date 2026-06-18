# sabnyx R1 Creation

A pocket capture client for the [Rabbit R1](https://www.rabbit.tech). A static
web app ("Creation") that runs in the R1's 240×282 WebView: hold PTT to speak,
the transcript is captured into your sabnyx vault (or answered from it), and the
R1 speaks the reply.

This is the **client half**. The **server half** is the HTTP ingress in the
sabnyx package (`src/sabnyx/commands/serve_http.py` + `adapters/r1.py`), run with
`sabnyx serve-http`. See `docs/recon/rabbit-r1-surface-2026.md` for the verified
device surface.

## The contract (client ⇄ server)

One small, stable JSON seam. Every request carries `Authorization: Bearer <key>`.

| Method | Route     | Body     | Returns                            |
| ------ | --------- | -------- | ---------------------------------- |
| POST   | `/ingest` | `{text}` | `{text, filed:[...]}`              |
| POST   | `/ask`    | `{text}` | `{text, cited:[...]}`              |
| GET    | `/status` | —        | `{intake, atlas, observations}`    |

Bad/missing key → `401`. Empty/oversized/invalid body → `400`/`413`.

## How voice works (and where it runs)

- PTT-**hold** (`longPressStart`/`longPressEnd`) → `CreationVoiceHandler` →
  **rabbit's cloud STT** → transcript via `onPluginMessage` (`{type:"sttEnded",
  transcript}`). The Creation then POSTs the **transcript text** — the sabnyx
  server never sees audio and runs no Whisper.
- The raw audio transits **rabbit's cloud** (the device↔`r1-api.rabbit.tech`
  channel). If you ever need to keep audio off rabbit's cloud, swap `voice.js`
  for `navigator.mediaDevices.getUserMedia` + your own ASR endpoint.
- TTS: `PluginMessageHandler` with `useLLM:false` speaks the reply **exactly** on
  the device speaker. Desktop falls back to `SpeechSynthesis`.
- Side button (single click): toggles **CAPTURE ⇄ ASK**.

> `CreationVoiceHandler`/`sttEnded` are undocumented in the official SDK
> reference but proven working in the SDK community's `gemma-chat` Creation.
> `creationStorage.secure` and `PluginMessageHandler` are officially documented.

## Setup

1. **Run the server** (on a host with TLS, e.g. behind nginx + Let's Encrypt):

   ```bash
   export SABNYX_R1_KEYS="$(openssl rand -hex 24)"   # one or more, comma-separated
   sabnyx serve-http --host 127.0.0.1 --port 8787    # refuses to run if KEYS empty
   ```

   Reverse-proxy `https://your-domain/` → `127.0.0.1:8787`.

2. **Point the client at it**: edit `BASE_URL` in `js/api.js` to your HTTPS
   origin (no trailing slash).

3. **Host the static files**: deploy this `creations/` directory to any static
   host (GitHub Pages, Netlify, or your own server — "plain HTML, no build
   step"). `index.html` is the Creation; `install.html` shows the QR.

4. **Install on the R1**: open `install.html`, scan the QR with your R1. On first
   run the Creation asks for one of your `SABNYX_R1_KEYS`; it's stored in
   `creationStorage.secure` (hardware-encrypted).

## Local testing (no device)

Open `index.html` in a browser. With no R1 bridge present it uses the typed
input, `localStorage` for the key, and `SpeechSynthesis` for replies — enough to
exercise the full request/response path against a running `sabnyx serve-http`.

## Files

| File           | Purpose                                                        |
| -------------- | ------------------------------------------------------------- |
| `index.html`   | The Creation UI (240×282): mode, status, log, PTT, fallback. |
| `install.html` | QR installer (points the R1 at `index.html`).                |
| `js/api.js`    | Backend client: key storage + `fetch` to the contract above. |
| `js/voice.js`  | PTT → rabbit cloud STT → transcript.                          |
| `js/speak.js`  | Reply TTS via the R1 bridge (`useLLM:false`).                |
