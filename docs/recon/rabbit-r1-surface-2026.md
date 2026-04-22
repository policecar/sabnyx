# Rabbit R1 developer surface for sabnyx (April 2026)

Source: rabbit-hmi-oss/creations-sdk on GitHub (official, MIT, last
commit Sep 2025 — "sneak-peek"). Primary evidence is the
`plugin-demo/` reference implementation plus
`reference/creation-triggers.md`.

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

**ASR**: PTT inside a Creation fires `sideClick` only — rabbit's
native assistant does NOT transcribe for us. We capture mic via
`getUserMedia` and ship raw audio to our server for Whisper.

**Camera**: same. `getUserMedia({video:true})`, grab a frame, POST
as base64 or multipart to our server for vision.

**TTS back to the user**: three options, in order of preference:
  1. Pre-generate TTS on the server (Kokoro / OpenAI TTS / ElevenLabs),
     return a URL, play via `<audio>`. Most control, zero LLM cost.
  2. `PluginMessageHandler.postMessage({message: "say exactly: <X>",
     useLLM: true, wantsR1Response: true})` — cheap but goes through
     their LLM and may not say exactly what we asked.
  3. Browser `SpeechSynthesis` — may or may not work in their WebView.

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
        v                                         |
    Creation WebView                              |
     getUserMedia(audio)                          |
        |                                         |
        | POST https://sabnyx.example.net/ingest   |
        | Authorization: Bearer <key>             |
        v                                         |
    sabnyx ingress (FastAPI, root server)         |
     whisper ASR --> NormalizedEnvelope           |
     dispatch -> sabnyx.commands.new/search/...   |
     write to ~/vault/..., git push               |
     TTS the reply audio                          |
     return {text, audio_url}                     |
        |                                         |
        +--> Creation receives JSON --> <audio> --+

Staging:

  1. Ingress on example.net (FastAPI + whisper-cpp or openai whisper +
     piper/kokoro TTS). nginx reverse-proxy, Let's Encrypt cert.
     Writes to a clone of the vault, git push on mutations.
  2. Minimal Creation (single index.html, one PTT-triggered flow).
     Deploy via rabbit.tech/creations.
  3. First envelope kind: content.submit (voice → intake/thought).
     Stage 2: query.search (voice → atlas grep → TTS reply).

## Unknowns still to verify

  - Does the R1 Chromium WebView grant mic/camera permission on
    first request, or does it need a special manifest entry? Test
    with a minimal Creation.
  - Latency round-trip expectations (R1 is on LTE/wifi). Likely
    2-4s end-to-end including ASR.
  - Does rabbit's Creation review process block "talks to arbitrary
    server" apps? Probably not (they explicitly promote standard
    web tech) but worth checking TOS before going public.

## Sources

  - github.com/rabbit-hmi-oss/creations-sdk (primary, MIT)
  - `plugin-demo/reference/creation-triggers.md` (auth+API spec)
  - `plugin-demo/js/speak.js`, `js/hardware.js`, `js/data.js`
    (working reference implementations)
  - rabbit.tech/creations (live page, not re-read for this report
    but listed as the official deployment surface)
