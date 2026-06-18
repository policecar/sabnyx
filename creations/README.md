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

1. **Generate a strong key.** This is the only thing between the public endpoint
   and your vault — make it unguessable, one per device so you can revoke
   individually:

   ```bash
   export SABNYX_R1_KEYS="$(openssl rand -hex 24)"   # 192-bit; comma-separate for more
   ```

2. **Run the server, serving this directory too.** One process, one origin — the
   page and the API share an origin, so CORS can't arise and one systemd unit
   covers both. `BASE_URL` in `js/api.js` is already `""` (relative), so it
   points at the same origin with no edit:

   ```bash
   sabnyx serve-http --static creations --host 127.0.0.1 --port 8787
   # refuses to start if SABNYX_R1_KEYS is empty
   ```

3. **Put TLS in front** (the R1 needs HTTPS). Either a tunnel — no open ports:

   ```bash
   cloudflared tunnel --url http://localhost:8787
   ```

   …or a one-line nginx terminator on your domain (opens 443), with a rate limit
   to blunt brute-force / floods:

   ```nginx
   limit_req_zone $binary_remote_addr zone=r1:10m rate=10r/s;   # in the http{} block
   server {
       listen 443 ssl;                       # + your certbot ssl_certificate lines
       server_name sabnyx.example.net;
       location / {
           limit_req zone=r1 burst=20 nodelay;
           proxy_pass http://127.0.0.1:8787;
       }
   }
   ```

4. **Install on the R1**: open `https://<your-origin>/install.html`, scan the QR.
   First run asks for one of your `SABNYX_R1_KEYS`; it's stored in
   `creationStorage.secure` (hardware-encrypted).

> **Security.** The static files are public *reads* and contain no secrets;
> vault *writes* require the bearer key, which lives only on your device — so
> scanning the QR alone can't write anything. Treat the key like any secret
> (`.env`, `chmod 600`, rotate if leaked). Only ever install from *your own*
> `install.html`.

## Local testing (no device)

Run `sabnyx serve-http --static creations` and open `http://localhost:8787/` in a
desktop browser. With no R1 bridge present it uses the typed input, `localStorage`
for the key, and `SpeechSynthesis` for replies — enough to exercise the full
request/response path end to end.

## Files

| File           | Purpose                                                        |
| -------------- | ------------------------------------------------------------- |
| `index.html`   | The Creation UI (240×282): mode, status, log, PTT, fallback. |
| `install.html` | QR installer (points the R1 at `index.html`).                |
| `js/api.js`    | Backend client: key storage + `fetch` to the contract above. |
| `js/voice.js`  | PTT → rabbit cloud STT → transcript.                          |
| `js/speak.js`  | Reply TTS via the R1 bridge (`useLLM:false`).                |
