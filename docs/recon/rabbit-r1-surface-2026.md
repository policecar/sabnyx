# Rabbit R1 developer surface for sabnyx (April 2026)

Source: rabbit-hmi-oss/creations-sdk on GitHub (official, MIT, last
commit Sep 2025 — "sneak-peek"). Primary evidence is the
`plugin-demo/` reference implementation plus
`reference/creation-triggers.md`.

**Verified 2026-06-18** against live source (the official `plugin-demo`
plus the community `andr3w-hilton/rabbit-r1-creations-public` Creations,
notably `gemma-chat`). Corrections from that pass are marked **[CORRECTED
2026-06-18]** / **[RESOLVED 2026-06-18]** inline. Short version: rabbit
transcribes voice for us — the Creation POSTs **text**, not audio, so the
server needs **no Whisper** — and `fetch()` to our own backend is proven
by working code.

## What a Creation actually is

A static HTML/CSS/JS web app deployed via rabbit.tech/creations. Runs
in a fixed 240x282 WebView on the device. Rabbit hosts it; we write
it. Installed to an R1 via a QR code flow.

The Creation has two channels:

  1. **Proprietary bridge** to rabbit's backend:
     - `PluginMessageHandler.postMessage(JSON)` — send a message,
       optionally with `useLLM:true`, `wantsR1Response:true` (TTS
       through R1 speaker), `wantsJournalEntry:true` (log to R1
       journal)
     - `window.onPluginMessage = (data) => {}` — receive response
     - `window.creationStorage.plain` / `.secure` — persistent
       per-plugin storage (hardware-encrypted in secure)
     - `window.creationSensors.accelerometer` — 6-axis data (tilt
       normalized + raw m/s²), configurable frequency 10-100 Hz
     - `TouchEventHandler` — simulate taps (accessibility/automation)
     - `closeWebView.postMessage("")` — exit
     - `CreationVoiceHandler.postMessage("start"|"stop")` — start/stop
       rabbit-side speech capture; the transcript returns through
       `window.onPluginMessage` as `{type:"sttEnded", transcript}`.
       **[CORRECTED 2026-06-18]** Undocumented in the official reference
       but proven working in the `gemma-chat` Creation.
     - Hardware event listeners:
       - `sideClick` (PTT button single click)
       - `longPressStart` / `longPressEnd` (PTT held)
       - `scrollUp` / `scrollDown` (scroll wheel)

  2. **Standard mobile web** (explicit in SDK README):
     microphone, camera, speaker. Which means:
     - `navigator.mediaDevices.getUserMedia({audio:true})` → voice
     - `navigator.mediaDevices.getUserMedia({video:true})` → camera
     - `fetch()` to any HTTPS endpoint we control
     - `<audio>` playback for pre-generated TTS
     - Possibly `SpeechSynthesis` / `SpeechRecognition` (not verified
       on R1's Chromium fork; safer to assume not)

**This is the key finding**: Creations can call arbitrary HTTPS
endpoints with arbitrary bodies. Our sabnyx ingress is the endpoint;
the R1 is a pocket capture device that fetches into it.

## Speech + vision

**ASR** **[CORRECTED 2026-06-18]**: the original claim here was wrong.
PTT-*hold* (`longPressStart`/`longPressEnd`), not `sideClick`, drives
voice, and rabbit DOES transcribe for us: `CreationVoiceHandler` starts/
stops capture and the transcript arrives via `onPluginMessage`
(`{type:"sttEnded", transcript}`). So the Creation POSTs **text** and the
server needs **no Whisper**. `sideClick` (single click) is therefore free
for other UI.

Note this ASR is rabbit's **cloud** STT (the device↔`wss://r1-api.rabbit.tech`
channel), not on-device. So two things follow: (1) the raw audio transits
rabbit's cloud — our server only ever sees the transcript text; (2) there's an
extra hop (device → rabbit cloud → device → our server), which the latency
estimate below accounts for. If keeping audio off rabbit's cloud ever matters,
the `getUserMedia` mic-capture fallback + our own ASR is the bypass.

The official reference still lists `getUserMedia` mic access
as a documented fallback if we ever want raw audio, but the bridge path is
simpler and proven.

**Camera**: same. `getUserMedia({video:true})`, grab a frame, POST
as base64 or multipart to our server for vision.

**TTS back to the user** **[CORRECTED 2026-06-18]**: simplest path first.
  1. `PluginMessageHandler.postMessage({message: <X>, useLLM: false,
     wantsR1Response: true})` — speaks our text **exactly** on the R1
     speaker (`useLLM:false` bypasses their LLM). Zero cost, proven in
     `gemma-chat`. This is now the default.
  2. Browser `SpeechSynthesis` — `gemma-chat` uses it as the fallback
     when the bridge is absent.
  3. Pre-generate TTS on the server (Kokoro / OpenAI TTS / ElevenLabs),
     return a URL, play via `<audio>`. Most control, but unnecessary now
     that on-device exact speech is confirmed.

## Auth + identity

No rabbit-provided user identity flows into a Creation. We handle
auth ourselves:
  - First run: user scans a QR code or taps through a setup flow to
    install a per-user API key into `window.creationStorage.secure`
  - Every `fetch` sends `Authorization: Bearer <key>` header
  - Server validates; key maps to a sabnyx vault (only one for now,
    but the shape scales)

Rabbit injects `pluginId` server-side to prevent spoofing, but that's
for the proprietary bridge, not our HTTPS calls.

## Screen constraint

240x282 px, portrait. Voice-first UI is easy: a big PTT affordance,
a status line, a tiny log. Anything richer wants the mobile app or
a laptop.

## Jailbreak path

Community effort exists (see r/Rabbitr1, GitHub "rabbit-r1" topic).
Unlocks: arbitrary APKs, background daemons, home-screen widgets,
direct hardware access, bypass of rabbit's Creation hosting. Also
voids warranty-ish and risks OTAs. For sabnyx specifically, the
stock Creation path gets us:
  - voice capture ✓
  - camera capture ✓
  - outbound HTTPS with auth ✓
  - hardware button events ✓
  - TTS playback (via `<audio>`) ✓
  - persistent secure storage ✓

The only things jailbreak adds that we'd care about:
  - "always listening" background daemon (power cost; probably not
    worth it)
  - home-screen tile that launches sabnyx directly (stock has plugin
    install → appears in launcher, good enough)
  - skipping rabbit's hosting (we'd need to self-host the static site
    otherwise; not a problem)

## Verdict for sabnyx integration

**Stock path. Weekend-viable.** Build a Creation + ingress and ship.

Architecture:

    [R1 PTT held]                            [R1 speaker]
        |                                         ^
        v                                         | PluginMessageHandler
    Creation WebView                              | (useLLM:false → exact)
     CreationVoiceHandler start/stop              |
     onPluginMessage sttEnded -> transcript       |
        |                                         |
        | POST https://sabnyx.example.net/ingest   |
        | Authorization: Bearer <key>             |
        | {text: transcript}                      |
        v                                         |
    sabnyx ingress (HTTP adapter, additive)       |
     transcript is already text — no Whisper      |
     -> NormalizedEnvelope(source="web")          |
     -> serve.handle(): services.answer_question /  |
                        build_ingest_proposal      |
     write to vault, git push                      |
     return {text, cited}                          |
        |                                         |
        +--> Creation receives JSON --> speak() ---+

Staging:

  1. Ingress on example.net (a small HTTP adapter over services.py — no
     Whisper, no server TTS; rabbit transcribes and the R1 speaks).
     nginx reverse-proxy, Let's Encrypt cert. Writes to the vault, git
     push on mutations. **[CORRECTED 2026-06-18: STT/ASR run in rabbit's
     cloud (not our server, not the device); the R1 only plays the reply.]**
  2. Minimal Creation (single index.html, one PTT-triggered flow).
     Deploy via rabbit.tech/creations.
  3. First envelope kind: content.submit (voice → intake/thought).
     Stage 2: query.search (voice → atlas grep → TTS reply).

## Unknowns still to verify

  - **[RESOLVED 2026-06-18]** `fetch()` to our own backend works —
    proven by `gemma-chat`: `fetch(PROXY_URL, {method:"POST",
    headers:{"Content-Type":"application/json"}, body: ...})`.
  - **[RESOLVED 2026-06-18]** Voice no longer needs `getUserMedia`
    permissions: the `CreationVoiceHandler` + `onPluginMessage`/`sttEnded`
    bridge does capture and ASR. (If we ever fall back to `getUserMedia`,
    its mic-permission prompt behavior remains untested.)
  - Latency round-trip expectations (R1 is on LTE/wifi). Likely
    2-4s end-to-end (ASR runs on rabbit's side).
  - Does rabbit's Creation review process block "talks to arbitrary
    server" apps? Probably not (they explicitly promote standard
    web tech) but worth checking TOS before going public.
  - Is `CreationVoiceHandler` stable? It is undocumented in the official
    reference; pin to the proven `gemma-chat` usage and keep
    `getUserMedia` as a documented fallback.

## Sources

  - github.com/rabbit-hmi-oss/creations-sdk (primary, MIT)
  - `plugin-demo/reference/creation-triggers.md` (auth+API spec)
  - `plugin-demo/js/speak.js`, `js/hardware.js`, `js/data.js`
    (working reference implementations)
  - rabbit.tech/creations (live page, not re-read for this report
    but listed as the official deployment surface)
  - github.com/andr3w-hilton/rabbit-r1-creations-public (community
    Creations, MIT) — `gemma-chat/index.html` + `gemma-chat/web-server.py`
    are the working evidence for `fetch()`-to-own-backend, the
    `CreationVoiceHandler`/`sttEnded` voice flow, and on-device
    `useLLM:false` TTS. Verified against live source 2026-06-18.
