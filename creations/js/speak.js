// sabnyx R1 Creation — text-to-speech.
//
// Prefers the R1 bridge with useLLM:false, which speaks our text EXACTLY on the
// device speaker (no LLM cost) — proven in the gemma-chat reference. Falls back
// to browser SpeechSynthesis for desktop testing.

function speak(text) {
  if (!text) return;
  if (typeof PluginMessageHandler !== "undefined") {
    PluginMessageHandler.postMessage(
      JSON.stringify({ message: text, useLLM: false, wantsR1Response: true }),
    );
    return;
  }
  if (window.speechSynthesis) {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05;
    window.speechSynthesis.speak(u);
  }
}

window.sabnyxSpeak = speak;
