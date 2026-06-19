// sabnyx R1 Creation — backend client.
//
// Talks to the sabnyx HTTP ingress (src/sabnyx/commands/serve_http.py) over
// plain fetch() — the channel proven by the SDK's gemma-chat reference.
//
// Key persistence is LAYERED, and the order matters. Every shipped self-hosted
// R1 Creation persists via plain `localStorage`; `window.creationStorage` is not
// confirmed to be injected for a self-hosted Creation (one installed via a QR to
// our own https URL, which ours is). So we write `localStorage` FIRST — the path
// proven to survive a relaunch — and ALSO upgrade to `creationStorage.secure`
// (hardware-encrypted, survives reinstall) when the bridge is present. On read we
// prefer `secure`, then fall back to `localStorage`. The earlier bug wrote one OR
// the other: when `secure` existed we returned before touching `localStorage`, so
// if `secure` didn't actually persist the key was lost on every relaunch.
// creationStorage values MUST be base64; getItem returns null when absent.
//
// Contract (see creations/README.md):
//   POST /ingest {text} -> {text, filed:[...]}
//   POST /ask    {text} -> {text, cited:[...]}
//   GET  /status        -> {intake, atlas, observations}

// Same origin by default — correct when the server runs with
// `serve-http --static creations` (recommended): the page and the API share one
// origin, so this stays "". Set a full "https://host" (no trailing slash) ONLY
// if you host the Creation separately from the API (then the server needs CORS).
const BASE_URL = "";

const KEY_NAME = "sabnyx_api_key";

function secureStore() {
  // The R1's hardware-encrypted persistent store, when the bridge is injected.
  if (typeof window.creationStorage !== "undefined" && window.creationStorage.secure) {
    return window.creationStorage.secure;
  }
  return null;
}

async function getKey() {
  const s = secureStore();
  if (s) {
    try {
      const v = await s.getItem(KEY_NAME); // null when absent
      if (v) return atob(v);
    } catch (e) {
      /* bridge flaky/absent — fall through to localStorage */
    }
  }
  try {
    return localStorage.getItem(KEY_NAME) || "";
  } catch (e) {
    return "";
  }
}

async function setKey(key) {
  // localStorage FIRST — the proven-persistent layer — so the key survives a
  // relaunch even if creationStorage is absent or doesn't persist self-hosted.
  try {
    localStorage.setItem(KEY_NAME, key);
  } catch (e) {
    /* ignore — try the secure store below */
  }
  const s = secureStore();
  if (s) {
    try {
      await s.setItem(KEY_NAME, btoa(key)); // creationStorage values must be base64
    } catch (e) {
      /* ignore — localStorage already holds it */
    }
  }
}

async function clearKey() {
  try {
    localStorage.removeItem(KEY_NAME);
  } catch (e) {
    /* ignore */
  }
  const s = secureStore();
  if (s) {
    try {
      await s.removeItem(KEY_NAME);
    } catch (e) {
      /* ignore */
    }
  }
}

function storageInfo() {
  // One-line diagnostic for the setup screen: what persistence is actually
  // available on this device. `cs` answers the open question — is
  // window.creationStorage injected for a self-hosted Creation?
  let ls = false;
  try {
    ls = typeof localStorage !== "undefined" && localStorage !== null;
  } catch (e) {
    ls = false;
  }
  const cs = typeof window.creationStorage !== "undefined";
  return "store: creationStorage " + (cs ? "✓" : "✗") + " · localStorage " + (ls ? "✓" : "✗");
}

async function call(method, path, body) {
  const key = await getKey();
  if (!key) {
    // No key stored (cleared, or the secure store came back empty). Fail with a
    // clear message instead of sending "Bearer " and surfacing a bare 401.
    throw new Error("no API key — reopen setup and paste your key");
  }
  const opts = { method, headers: { Authorization: "Bearer " + key } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(BASE_URL + path, opts);
  let data = {};
  try {
    data = await r.json();
  } catch (e) {
    /* non-JSON / empty body */
  }
  if (!r.ok) {
    throw new Error(data.error || "HTTP " + r.status);
  }
  return data;
}

window.sabnyxApi = {
  getKey,
  setKey,
  clearKey,
  storageInfo,
  ingest: (text) => call("POST", "/ingest", { text }),
  ask: (text) => call("POST", "/ask", { text }),
  status: () => call("GET", "/status"),
};
