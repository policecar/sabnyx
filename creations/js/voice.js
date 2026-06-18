// sabnyx R1 Creation — voice capture.
//
// PTT-hold (longPressStart/longPressEnd) drives rabbit's CLOUD STT via
// CreationVoiceHandler; the transcript returns through window.onPluginMessage as
// {type:"sttEnded", transcript}. (CreationVoiceHandler is undocumented in the
// official reference but proven in the gemma-chat Creation — see
// docs/recon/rabbit-r1-surface-2026.md. The raw audio transits rabbit's cloud;
// we only ever receive text.)
//
// wireVoice(onTranscript, onState):
//   onTranscript(text) — called with the final transcript
//   onState(state)     — "listening" | "idle" (optional, for UI)

function wireVoice(onTranscript, onState) {
  let listening = false;

  function setListening(v) {
    listening = v;
    if (onState) onState(v ? "listening" : "idle");
  }

  window.addEventListener("longPressStart", () => {
    if (listening) return;
    setListening(true);
    if (typeof CreationVoiceHandler !== "undefined") {
      CreationVoiceHandler.postMessage("start");
    }
  });

  window.addEventListener("longPressEnd", () => {
    if (!listening) return;
    if (typeof CreationVoiceHandler !== "undefined") {
      CreationVoiceHandler.postMessage("stop");
    }
  });

  // Chain onto any existing onPluginMessage (speak.js / others may share it).
  const prev = window.onPluginMessage;
  window.onPluginMessage = (data) => {
    let d = data;
    if (typeof d === "string") {
      try {
        d = JSON.parse(d);
      } catch (e) {
        /* leave as string */
      }
    }
    if (d && d.type === "sttEnded") {
      setListening(false);
      const t = (d.transcript || "").trim();
      if (t) onTranscript(t);
      return;
    }
    if (typeof prev === "function") prev(data);
  };
}

window.sabnyxWireVoice = wireVoice;
