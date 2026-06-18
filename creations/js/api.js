// sabnyx R1 Creation — backend client.
//
// Talks to the sabnyx HTTP ingress (src/sabnyx/commands/serve_http.py) over
// plain fetch() — the channel proven by the SDK's gemma-chat reference. The
// bearer key lives in window.creationStorage.secure (hardware-encrypted on the
// R1; base64 values per the official API), with a localStorage fallback for
// desktop testing.
//
// Contract (see creations/README.md):
//   POST /ingest {text} -> {text, filed:[...]}
//   POST /ask    {text} -> {text, cited:[...]}
//   GET  /status        -> {intake, atlas, observations}

// EDIT THIS: your deployed ingress, behind nginx + TLS. No trailing slash.
const BASE_URL = "https://sabnyx.example.net";

const KEY_NAME = "sabnyx_api_key";

async function getKey() {
  try {
    if (window.creationStorage && window.creationStorage.secure) {
      const v = await window.creationStorage.secure.getItem(KEY_NAME);
      return v ? atob(v) : "";
    }
  } catch (e) {
    /* fall through to localStorage */
  }
  return localStorage.getItem(KEY_NAME) || "";
}

async function setKey(key) {
  try {
    if (window.creationStorage && window.creationStorage.secure) {
      await window.creationStorage.secure.setItem(KEY_NAME, btoa(key));
      return;
    }
  } catch (e) {
    /* fall through to localStorage */
  }
  localStorage.setItem(KEY_NAME, key);
}

async function clearKey() {
  try {
    if (window.creationStorage && window.creationStorage.secure) {
      await window.creationStorage.secure.removeItem(KEY_NAME);
    }
  } catch (e) {
    /* ignore */
  }
  localStorage.removeItem(KEY_NAME);
}

async function call(method, path, body) {
  const key = await getKey();
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
  ingest: (text) => call("POST", "/ingest", { text }),
  ask: (text) => call("POST", "/ask", { text }),
  status: () => call("GET", "/status"),
};
