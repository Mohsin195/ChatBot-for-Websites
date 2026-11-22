"use strict";

const $ = (id) => document.getElementById(id);
const state = { siteId: null, siteName: "", history: [], busy: false, poll: null, audio: null, audioBtn: null, speech: null, listening: false };
const STAGES = ["discovering", "crawling", "indexing", "ready"];
const STAGE_LABEL = {
  queued: "Waiting in line", discovering: "Discovering pages", crawling: "Crawling pages", indexing: "Building knowledge base",
  ready: "Ready", error: "Something went wrong",
};
const SPEAKER_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/></svg>';
const STOP_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

function show(screen) {
  for (const id of ["start", "build", "chat"]) $(id).hidden = id !== screen;
}

async function post(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

async function errorText(res) {
  try {
    const j = await res.json();
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail) && j.detail[0]) return j.detail[0].msg;
  } catch { /* not JSON */ }
  return `Request failed (HTTP ${res.status}).`;
}

/* ---------- Safe markdown: escape first, then add a small set of formatting ---------- */
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Citation markers like [2] or [1, 3] become a small link icon; hovering shows the page, clicking opens it.
// The text is already HTML-escaped, so source titles and URLs are escaped before going into attributes.
function citations(s, sources) {
  // Trailing punctuation joins the icons so a line never wraps between the icon and its period.
  return s.replace(/\s*((?:\[\d{1,2}(?:\s*,\s*\d{1,2})*\])+)(?![(\w])([.,;:!?]?)/g, (_, group, punct) => {
    const nums = [...new Set(group.match(/\d+/g).map(Number))];
    const links = nums.map((n) => sources && sources[n]).filter(Boolean).map((src) =>
      `<a class="cite" href="${escapeHtml(src.url)}" target="_blank" rel="noopener noreferrer"` +
      ` data-title="${escapeHtml(src.title || "")}" aria-label="Source: ${escapeHtml(src.title || src.url)}">🔗</a>`);
    return links.length ? `<span class="cites">${links.join("")}${punct}</span>` : punct;
  });
}

function inline(s, sources) {
  const a = (href, text) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  return citations(s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, h) => a(h, t))
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]*[^\s<).,;:!?])/g, (_, p, h) => p + a(h, h))
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>"), sources);
}

function renderMarkdown(md, sources) {
  const fmt = (s) => inline(s, sources);
  let html = "", list = null, para = [];
  const flush = () => { if (para.length) { html += `<p>${fmt(para.join("<br>"))}</p>`; para = []; } };
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  const item = (tag, text) => {
    flush();
    if (list !== tag) { close(); html += `<${tag}>`; list = tag; }
    html += `<li>${fmt(text)}</li>`;
  };
  for (const line of escapeHtml(md).split("\n")) {
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) item("ul", m[1]);
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) item("ol", m[1]);
    else if ((m = line.match(/^\s*#{1,6}\s+(.*)/))) { flush(); close(); html += `<h4>${fmt(m[1])}</h4>`; }
    else if (!line.trim()) { flush(); close(); }
    else { close(); para.push(line.trim()); }
  }
  flush(); close();
  return html;
}

/* ---------- Start screen ---------- */
function formError(msg) {
  $("form-error").textContent = msg;
  $("form-error").hidden = !msg;
  $("url-field").classList.toggle("is-invalid", !!msg);
  if (msg) $("url").setAttribute("aria-invalid", "true");
  else $("url").removeAttribute("aria-invalid");
}

// A light client-side check for obvious typos; the server does the real validation and normalization.
function looksLikeUrl(value) {
  const v = value.trim();
  if (!v || /\s/.test(v)) return false;
  try {
    const u = new URL(/^[a-z][a-z\d+.-]*:\/\//i.test(v) ? v : `https://${v}`);
    return /^https?:$/.test(u.protocol) && /^[^.]+(\.[^.]+)+$/.test(u.hostname) && !/\.$/.test(u.hostname);
  } catch { return false; }
}

$("url").addEventListener("input", () => {
  const v = $("url").value;
  $("url-field").classList.toggle("is-valid", looksLikeUrl(v));
  if (!$("form-error").hidden) formError("");
});

// Max pages stepper. The server's limit arrives with /api/status (applyLimits); the server enforces it too.
let pageLimit = 500;
function clampPages(n) { return Math.min(pageLimit, Math.max(1, n)); }
function applyLimits(s) {
  if (!s || !s.max_pages_limit) return;
  pageLimit = s.max_pages_limit;
  const input = $("max-pages");
  input.max = pageLimit;
  if (!input.dataset.touched) input.value = Math.min(s.default_max_pages || pageLimit, pageLimit);
  input.value = clampPages(parseInt(input.value, 10) || pageLimit);
  $("pages-limit-note").textContent = `Up to ${pageLimit.toLocaleString()} pages`;
}
$("max-pages").addEventListener("input", () => { $("max-pages").dataset.touched = "1"; });
document.querySelectorAll(".step-btn").forEach((b) => b.addEventListener("click", () => {
  const cur = parseInt($("max-pages").value, 10) || 500;
  const step = Number(b.dataset.step);
  // Snap to multiples of 100 so 500 -> 600 and 1 -> 100 feel natural.
  const next = step > 0 ? Math.floor(cur / 100) * 100 + 100 : Math.ceil(cur / 100) * 100 - 100;
  $("max-pages").value = clampPages(next);
}));
$("max-pages").addEventListener("blur", () => {
  const n = parseInt($("max-pages").value, 10);
  $("max-pages").value = clampPages(Number.isFinite(n) ? n : 500);
});

function setBuilding(on) {
  const btn = $("build-btn");
  btn.disabled = on;
  btn.classList.toggle("is-loading", on);
  btn.setAttribute("aria-busy", String(on));
  btn.querySelector(".btn-label").textContent = on ? "Starting crawl…" : "Build My AI Assistant";
  $("crawl-form").classList.toggle("is-busy", on);
}

$("crawl-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  formError("");
  const url = $("url").value.trim();
  if (!url) { formError("Please enter a website URL."); $("url").focus(); return; }
  if (!looksLikeUrl(url)) { formError("That doesn't look like a website address. Try something like example.com."); $("url").focus(); return; }
  const maxPages = clampPages(parseInt($("max-pages").value, 10) || pageLimit);
  $("max-pages").value = maxPages;
  setBuilding(true);
  try {
    const res = await post("/api/crawl", { url, max_pages: maxPages });
    if (!res.ok) { formError(await errorText(res)); return; }
    const { site_id } = await res.json();
    state.siteId = site_id;
    resetChat();
    $("build-site").textContent = url.replace(/^https?:\/\//i, "").replace(/\/$/, "");
    startPolling();
  } catch {
    formError("Could not reach the SiteSense server. Is it running?");
  } finally {
    setBuilding(false);
  }
});

// The full placeholder is cut off on phones; use a shorter one there.
const narrow = matchMedia("(max-width: 600px)");
const setPlaceholder = () => { $("url").placeholder = narrow.matches ? "yourwebsite.com" : "Enter your website URL — example.com"; };
narrow.addEventListener("change", setPlaceholder);
setPlaceholder();

function focusUrl() {
  window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  $("url").focus({ preventScroll: true });
}
$("nav-start").addEventListener("click", focusUrl);
$("band-start").addEventListener("click", focusUrl);

/* ---------- Landing page: nav surface, scroll reveals, theme ---------- */
const nav = $("nav");
const onScroll = () => nav.classList.toggle("is-scrolled", window.scrollY > 8);
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      if (en.isIntersecting) { en.target.classList.add("is-in"); io.unobserve(en.target); }
    }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
  document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
} else {
  document.querySelectorAll(".reveal").forEach((el) => el.classList.add("is-in"));
}

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function syncThemeButtons() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.querySelectorAll(".theme-toggle").forEach((b) => b.setAttribute("aria-label", `Switch to ${next} theme`));
}
document.querySelectorAll(".theme-toggle").forEach((b) => b.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch { /* storage blocked */ }
  syncThemeButtons();
}));
syncThemeButtons();

/* ---------- Build progress ---------- */
function startPolling() {
  stopPolling();
  show("build");
  renderBuild({ stage: "discovering", pages_found: 0, pages_crawled: 0, chunks_indexed: 0, message: "Starting…" });
  state.poll = setInterval(refreshStatus, 1000);
  refreshStatus();
}

function stopPolling() {
  clearInterval(state.poll);
  state.poll = null;
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`Status request failed (${response.status})`);
    const s = await response.json();
    if (!state.poll) return;
    if (!s.site_id) {
      $("build-message").textContent = "The server could not find this crawl. Retrying… You can cancel and try again if progress does not resume.";
      return;
    }
    state.siteId = s.site_id;
    renderBuild(s);
    if (s.stage === "ready") { stopPolling(); enterChat(s); }
    if (s.stage === "error") stopPolling();
  } catch {
    $("build-message").textContent = "Lost connection to the server. Retrying…";
  }
}

function progressPercent(s) {
  if (s.stage === "ready") return 100;
  if (s.stage === "crawling") {
    // Whichever finishes first ends the crawl: the discovered queue, or the page cap.
    const queueDone = s.pages_crawled / Math.max(1, s.pages_found);
    const capDone = s.max_pages ? (s.pages_indexed || 0) / s.max_pages : 0;
    return 5 + 65 * Math.min(1, Math.max(queueDone, capDone));
  }
  if (s.stage === "indexing") return 70 + 28 * (s.chunks_total ? s.chunks_indexed / s.chunks_total : 0);
  return 0;
}

function renderBuild(s) {
  const card = document.querySelector(".progress-card");
  const isError = s.stage === "error";
  card.classList.toggle("is-error", isError);
  $("build-stage").textContent = STAGE_LABEL[s.stage] || s.stage;
  if (s.site_url) $("build-site").textContent = s.site_url.replace(/^https?:\/\//, "").replace(/\/$/, "");
  $("stat-found").textContent = s.pages_found.toLocaleString();
  $("stat-crawled").textContent = s.pages_crawled.toLocaleString();
  $("stat-chunks").textContent = s.chunks_indexed.toLocaleString();
  $("build-message").textContent = s.message || "";
  $("cancel-build").textContent = isError ? "Try another website" : "Cancel";

  const current = STAGES.indexOf(s.stage);
  document.querySelectorAll(".steps li").forEach((li, i) => {
    li.classList.toggle("done", current > i || s.stage === "ready");
    li.classList.toggle("active", current === i);
  });
  const pct = Math.round(progressPercent(s));
  $("bar").classList.toggle("indeterminate", s.stage === "discovering" || s.stage === "queued");
  $("bar").setAttribute("aria-valuenow", pct);
  $("bar-fill").style.width = `${isError ? 100 : pct}%`;
}

$("cancel-build").addEventListener("click", resetEverything);

/* ---------- Chat ---------- */
function enterChat(s) {
  state.siteId = s.site_id;
  state.siteName = s.site_name;
  $("site-name").textContent = s.site_name;
  $("site-url").textContent = s.site_url.replace(/^https?:\/\//, "").replace(/\/$/, "");
  $("site-url").href = s.site_url;
  $("welcome-name").textContent = s.site_name;
  $("welcome-meta").textContent = `Answers are grounded in ${s.pages_indexed.toLocaleString()} pages from this website.`;

  const img = $("site-icon"), initial = $("site-initial");
  initial.textContent = (s.site_name || "?").trim().charAt(0).toUpperCase();
  img.hidden = false; initial.hidden = true;
  img.onerror = () => { img.hidden = true; initial.hidden = false; };
  img.src = s.icon || "";
  if (!s.icon) img.onerror();

  const starters = $("starters");
  starters.replaceChildren();
  for (const topic of s.topics || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "starter";
    b.textContent = `Tell me about ${topic}`;
    b.addEventListener("click", () => ask(b.textContent));
    starters.append(b);
  }
  show("chat");
  $("question").focus();
}

function resetChat() {
  stopAudio();
  state.history = [];
  $("messages").querySelectorAll(".msg").forEach((n) => n.remove());
  $("welcome").hidden = false;
  $("question").value = "";
}

function addMessage(role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const body = document.createElement("div");
  if (role === "user") body.textContent = text;
  else body.innerHTML = '<span class="typing" aria-label="Thinking"><i></i><i></i><i></i></span>';
  bubble.append(body);
  wrap.append(bubble);
  $("messages").append(wrap);
  scrollDown();
  return { bubble, body };
}

function scrollDown() {
  const m = $("messages");
  m.scrollTop = m.scrollHeight;
}

// The answer footer holds only the read-aloud button. Sources are shown solely as the
// inline citation links inside the answer text; there is no "Sources" list under answers.
function finishAnswer(msg, text) {
  const foot = document.createElement("div");
  foot.className = "answer-foot";
  const speak = document.createElement("button");
  speak.type = "button";
  speak.className = "speak";
  speak.innerHTML = SPEAKER_ICON;
  speak.setAttribute("aria-label", "Read answer aloud");
  speak.setAttribute("aria-pressed", "false");
  speak.addEventListener("click", () => toggleSpeech(speak, text));
  foot.append(speak);
  msg.bubble.append(foot);
  scrollDown();
  if ($("auto-read").checked) toggleSpeech(speak, text);
}

function showError(msg, text) {
  msg.bubble.classList.add("error");
  msg.body.textContent = text;
}

async function ask(question) {
  question = question.trim();
  if (!question || state.busy) return;
  state.busy = true;
  $("send").disabled = true;
  $("welcome").hidden = true;
  addMessage("user", question);
  const msg = addMessage("assistant");
  let answer = "", finished = false, cited = {};

  try {
    const res = await post("/api/chat", { site_id: state.siteId, question, history: state.history.slice(-8) });
    if (!res.ok) { showError(msg, await errorText(res)); return; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        if (!raw.startsWith("data: ")) continue;
        const ev = JSON.parse(raw.slice(6));
        if (ev.type === "sources") {
          cited = Object.fromEntries((ev.sources || []).map((src) => [src.n, src]));
        } else if (ev.type === "token") {
          answer += ev.text;
          msg.bubble.classList.add("is-streaming");
          msg.body.innerHTML = renderMarkdown(answer, cited);
          scrollDown();
        } else if (ev.type === "done") {
          finished = true;
          state.history.push({ role: "user", content: question }, { role: "assistant", content: answer });
          finishAnswer(msg, answer);
        } else if (ev.type === "error") {
          finished = true;
          showError(msg, ev.message);
        }
      }
    }
    if (!finished) showError(msg, "The answer was interrupted. Please try again.");
  } catch {
    showError(msg, "Connection lost. Please check the server and try again.");
  } finally {
    msg.bubble.classList.remove("is-streaming");
    state.busy = false;
    $("send").disabled = false;
    $("question").focus();
  }
}

const question = $("question");
$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = question.value;
  question.value = "";
  autoGrow();
  ask(q);
});
question.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("ask-form").requestSubmit();
  }
});
function autoGrow() {
  question.style.height = "auto";
  question.style.height = `${Math.min(question.scrollHeight, 180)}px`;
}
question.addEventListener("input", autoGrow);

/* ---------- Speech to text ---------- */
// The recording is the main path: it is sent to the server, which transcribes it with the website's own
// vocabulary. The browser's built-in recognizer (Chrome/Edge) only adds live text while you speak. It relies
// on an online speech service that is often unreachable (company networks, VPNs, some browsers), so its
// failures never stop the recording. Recording stops when you tap the mic again, after a pause in speech,
// or after 30 seconds.
const mic = $("mic");
const composerNote = $("composer-note");
const defaultComposerNote = composerNote.textContent;
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const canRecord = !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
const MAX_RECORDING_MS = 30000;
const SILENCE_STOP_MS = 1600;     // stop this long after the speaker goes quiet
const NO_SPEECH_STOP_MS = 8000;   // give up if nothing is said at all
const MIN_RECORDING_MS = 600;
const REFINING_NOTE = "Transcribing…";
const voice = {
  recognition: null, recognitionUsable: true,
  recorder: null, stream: null, audioCtx: null, vadTimer: null, timer: null,
  session: 0, baseText: "", startedAt: 0, heardSpeech: false, lastSoundAt: 0,
};

function note(text) { composerNote.textContent = text; }
function resetNote() { note(defaultComposerNote); }

function setListening(on) {
  state.listening = on;
  mic.classList.toggle("is-listening", on);
  mic.setAttribute("aria-pressed", String(on));
  mic.setAttribute("aria-label", on ? "Stop recording" : "Ask by voice");
  mic.title = on ? "Stop recording" : "Ask by voice";
}

if (!canRecord && !SpeechRecognition) {
  mic.disabled = true;
  mic.title = window.isSecureContext
    ? "Voice input is not supported by this browser. Try Chrome, Edge or Safari."
    : "Voice input needs a secure (https) connection.";
  mic.setAttribute("aria-label", mic.title);
  note(`${defaultComposerNote} ${mic.title}`);
} else {
  if (SpeechRecognition) {
    const recognition = new SpeechRecognition();
    voice.recognition = state.speech = recognition;
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = true;
    recognition.continuous = true;

    recognition.onresult = (event) => {
      let finalText = "", interimText = "";
      for (let i = 0; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interimText += result[0].transcript;
      }
      question.value = [voice.baseText, finalText, interimText].filter(Boolean).join(" ").slice(0, 2000);
      autoGrow();
    };
    recognition.onerror = (event) => {
      if (event.error === "aborted") return;
      // The online speech service is unreachable or refused: stop using it on this page; the recording carries on.
      if (["network", "service-not-allowed", "language-not-supported", "audio-capture"].includes(event.error)) {
        voice.recognitionUsable = false;
      }
      if (voice.recorder) return; // the server transcript still arrives
      if (event.error === "not-allowed") note("Microphone access was blocked. Allow it in your browser's site settings, then try again.");
      else if (event.error === "no-speech") note("No speech detected. Tap the microphone and try again.");
      else if (event.error === "network") note("The browser's speech service couldn't be reached. Check your connection and try again.");
      else note("Voice input failed. Please try again.");
    };
    // Without a recording, the recognizer's end is the end of the question.
    // With a recording, silence detection or a tap decides; a recognizer failure must not cut it short.
    recognition.onend = () => { if (state.listening && !voice.recorder) stopVoice(); };
  }

  mic.addEventListener("click", () => (state.listening ? stopVoice() : startVoice()));
}

function micErrorMessage(err) {
  switch (err && err.name) {
    case "NotAllowedError": case "SecurityError":
      return "Microphone access was blocked. Click the icon in the address bar to allow the microphone, then try again.";
    case "NotFoundError": case "OverconstrainedError":
      return "No microphone was found. Connect a microphone and try again.";
    case "NotReadableError": case "AbortError":
      return "The microphone is being used by another app or couldn't start. Close other apps using it and try again.";
    default:
      return "";
  }
}

// Watches the input level so recording ends on its own when the speaker stops talking.
function startSilenceDetection(stream, session) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  try {
    const ctx = new Ctx();
    voice.audioCtx = ctx;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const data = new Float32Array(analyser.fftSize);
    let noise = 0.004; // adapts to the room's background level
    voice.vadTimer = setInterval(() => {
      if (session !== voice.session || !state.listening) return;
      analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length);
      const now = performance.now();
      if (rms > Math.max(0.012, noise * 3)) {
        voice.heardSpeech = true;
        voice.lastSoundAt = now;
      } else {
        noise = noise * 0.95 + rms * 0.05;
      }
      if (voice.heardSpeech && now - voice.lastSoundAt > SILENCE_STOP_MS) stopVoice();
      else if (!voice.heardSpeech && now - voice.startedAt > NO_SPEECH_STOP_MS) {
        voice.noSpeech = true;
        stopVoice();
      }
    }, 100);
  } catch { /* no silence detection: a tap or the 30 s limit ends the recording */ }
}

async function startVoice() {
  const session = ++voice.session;
  voice.baseText = question.value.trim();
  voice.heardSpeech = false;
  voice.noSpeech = false;
  voice.startedAt = voice.lastSoundAt = performance.now();
  setListening(true);
  note("Starting microphone…");

  if (canRecord) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (session !== voice.session || !state.listening) { stream.getTracks().forEach((t) => t.stop()); return; }
      const chunks = [];
      const recorder = new MediaRecorder(stream);
      const startedAt = performance.now();
      recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = () => refineTranscript(session, new Blob(chunks, { type: recorder.mimeType || "audio/webm" }), performance.now() - startedAt);
      recorder.start(250);
      voice.stream = stream;
      voice.recorder = recorder;
      voice.startedAt = voice.lastSoundAt = startedAt;
      startSilenceDetection(stream, session);
    } catch (err) {
      const msg = micErrorMessage(err);
      if (msg) { setListening(false); note(msg); return; }
      voice.recorder = null; // recording unavailable: fall back to the browser recognizer alone
    }
  }

  if (voice.recognition && voice.recognitionUsable) {
    try { voice.recognition.start(); } catch { /* already running */ }
  } else if (!voice.recorder) {
    setListening(false);
    note("Voice input isn't available right now. Please type your question.");
    return;
  }
  note("Listening… speak your question. Tap the microphone when you're done.");
  clearTimeout(voice.timer);
  voice.timer = setTimeout(() => { if (state.listening && session === voice.session) stopVoice(); }, MAX_RECORDING_MS);
}

function stopVoice() {
  clearTimeout(voice.timer);
  clearInterval(voice.vadTimer);
  voice.vadTimer = null;
  if (voice.audioCtx) { voice.audioCtx.close().catch(() => {}); voice.audioCtx = null; }
  setListening(false);
  if (voice.recognition) { try { voice.recognition.stop(); } catch { /* not running */ } }
  if (voice.recorder && voice.recorder.state !== "inactive") voice.recorder.stop();
  if (voice.stream) voice.stream.getTracks().forEach((t) => t.stop());
  voice.recorder = voice.stream = null;
  if (composerNote.textContent.startsWith("Listening") || composerNote.textContent.startsWith("Starting")) resetNote();
}

async function refineTranscript(session, blob, durationMs) {
  const hasLiveText = question.value.trim() !== voice.baseText;
  if (!state.siteId) return;
  if (voice.noSpeech && !hasLiveText) {
    note("No speech detected. Check that the right microphone is selected and try again.");
    return;
  }
  if (durationMs < MIN_RECORDING_MS || blob.size < 1000) {
    if (!hasLiveText) note("That was too short. Tap the microphone and speak your question.");
    return;
  }
  const draft = question.value;
  note(REFINING_NOTE);
  question.classList.add("is-refining");
  let problem = "";
  try {
    let audio;
    try { audio = await toWav(blob); } catch { audio = blob; } // the server also accepts webm/ogg/mp4
    const type = (audio.type || "audio/webm").split(";")[0];
    const res = await fetch(`/api/transcribe?site_id=${encodeURIComponent(state.siteId)}`, {
      method: "POST", headers: { "Content-Type": type }, body: audio,
    });
    if (!res.ok) { problem = await errorText(res); return; }
    const { text } = await res.json();
    if (!text && !hasLiveText) { problem = "Sorry, I didn't catch that. Please try again."; return; }
    // Keep whatever the user did meanwhile: a new recording, edits, or sending the question.
    if (text && session === voice.session && !state.listening && question.value === draft) {
      question.value = [voice.baseText, text].filter(Boolean).join(" ").slice(0, 2000);
      autoGrow();
      question.focus();
    }
  } catch {
    problem = "Couldn't reach the server to transcribe your question. Please try again.";
  } finally {
    question.classList.remove("is-refining");
    if (composerNote.textContent === REFINING_NOTE) {
      if (problem) note(hasLiveText ? `${problem} The live transcript was kept.` : problem);
      else resetNote();
    }
  }
}

// Compressed browser audio -> 16 kHz mono WAV: small, and a format every speech model accepts.
async function toWav(blob) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const rate = 16000;
    const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
    const src = offline.createBufferSource();
    src.buffer = decoded;
    src.connect(offline.destination);
    src.start();
    const pcm = (await offline.startRendering()).getChannelData(0);
    const view = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const ascii = (offset, s) => { for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i)); };
    ascii(0, "RIFF"); view.setUint32(4, 36 + pcm.length * 2, true); ascii(8, "WAVE");
    ascii(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    ascii(36, "data"); view.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
      const x = Math.max(-1, Math.min(1, pcm[i]));
      view.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
    return new Blob([view.buffer], { type: "audio/wav" });
  } finally {
    ctx.close();
  }
}

/* ---------- Citation tooltip ---------- */
// One shared tooltip, positioned in JS so it never spills off-screen near the bubble edges.
const tip = document.createElement("div");
tip.className = "cite-tip";
tip.setAttribute("role", "tooltip");
tip.hidden = true;
document.body.append(tip);

function showTip(link) {
  const title = link.dataset.title;
  tip.replaceChildren();
  if (title) { const t = document.createElement("strong"); t.textContent = title; tip.append(t); }
  const u = document.createElement("span");
  u.textContent = link.href.replace(/^https?:\/\//, "");
  tip.append(u);
  tip.hidden = false;
  const r = link.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight, pad = 8;
  const left = Math.min(Math.max(pad, r.left + r.width / 2 - w / 2), window.innerWidth - w - pad);
  const top = r.top - h - 8 >= pad ? r.top - h - 8 : r.bottom + 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
function hideTip() { tip.hidden = true; }

$("messages").addEventListener("mouseover", (e) => { const a = e.target.closest(".cite"); if (a) showTip(a); });
$("messages").addEventListener("mouseout", (e) => { if (e.target.closest(".cite")) hideTip(); });
$("messages").addEventListener("focusin", (e) => { const a = e.target.closest(".cite"); if (a) showTip(a); });
$("messages").addEventListener("focusout", hideTip);
$("messages").addEventListener("scroll", hideTip, { passive: true });

/* ---------- Text to speech ---------- */
// The server's neural voice is used first. If it fails (voice service blocked, rate limit, offline), the
// browser's built-in speech (Web Speech API) reads the answer instead: free, on-device, no server needed.
const synth = "speechSynthesis" in window ? window.speechSynthesis : null;
let browserSpeaking = false;
if (synth) { synth.getVoices(); synth.addEventListener?.("voiceschanged", () => synth.getVoices()); }

function cleanForSpeech(text) {
  return text
    .replace(/\s*\[\d{1,2}(?:\s*,\s*\d{1,2})*\]/g, "")   // citation markers
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")              // [label](url) -> label
    .replace(/https?:\/\/\S+|www\.\S+/g, "")
    .replace(/^\s*(#+|[-*+]|\d+\.)\s+/gm, "")              // headings and list markers
    .replace(/[*_`#>|~]+/g, "")
    .replace(/\s+/g, " ").trim();
}

function pickVoice() {
  const voices = synth.getVoices();
  const lang = (navigator.language || "en-US").toLowerCase();
  const base = lang.split("-")[0];
  const matching = voices.filter((v) => v.lang.toLowerCase().startsWith(base));
  const natural = (v) => /natural|neural|online|google|enhanced|premium/i.test(v.name);
  return matching.find((v) => v.lang.toLowerCase() === lang && natural(v))
      || matching.find(natural) || matching.find((v) => v.lang.toLowerCase() === lang) || matching[0] || null;
}

function speakInBrowser(btn, text) {
  if (!synth) return false;
  const clean = cleanForSpeech(text).slice(0, 5000);
  if (!clean) return false;
  synth.cancel();
  // Short utterances: some browsers stop long ones after ~15 seconds.
  const parts = clean.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [clean];
  const chunks = [];
  for (const p of parts) {
    if (chunks.length && (chunks[chunks.length - 1] + p).length < 220) chunks[chunks.length - 1] += p;
    else chunks.push(p);
  }
  const voice = pickVoice();
  chunks.forEach((chunk, i) => {
    const u = new SpeechSynthesisUtterance(chunk.trim());
    if (voice) { u.voice = voice; u.lang = voice.lang; } else { u.lang = navigator.language || "en-US"; }
    if (i === chunks.length - 1) u.onend = () => { if (state.audioBtn === btn) stopAudio(); };
    u.onerror = (e) => { if (e.error !== "interrupted" && e.error !== "canceled" && state.audioBtn === btn) stopAudio(); };
    synth.speak(u);
  });
  browserSpeaking = true;
  btn.setAttribute("aria-pressed", "true");
  btn.innerHTML = STOP_ICON;
  btn.title = "Reading with your browser's voice";
  return true;
}

function stopAudio() {
  if (state.audio) { state.audio.pause(); state.audio.removeAttribute("src"); state.audio.load(); }
  const btn = state.audioBtn;
  state.audio = state.audioBtn = null;
  if (browserSpeaking && synth) { browserSpeaking = false; synth.cancel(); }
  if (btn) { btn.setAttribute("aria-pressed", "false"); btn.innerHTML = SPEAKER_ICON; }
}

async function toggleSpeech(btn, text) {
  if (state.audioBtn === btn) { stopAudio(); return; }
  stopAudio();
  state.audioBtn = btn;
  btn.setAttribute("aria-busy", "true");
  try {
    const res = await post("/api/tts", { text });
    if (!res.ok) throw new Error(await errorText(res));
    const { url } = await res.json();
    if (state.audioBtn !== btn) return; // user clicked elsewhere meanwhile
    const audio = new Audio(url); // streams, so playback starts before the whole answer is synthesized
    state.audio = audio;
    btn.setAttribute("aria-pressed", "true");
    btn.innerHTML = STOP_ICON;
    audio.addEventListener("ended", stopAudio);
    await audio.play();
  } catch (err) {
    if (state.audioBtn !== btn) return; // user stopped or switched answers meanwhile
    if (state.audio) { state.audio.pause(); state.audio = null; }
    if (speakInBrowser(btn, text)) return;
    stopAudio();
    btn.title = err.message || "Could not play audio.";
  } finally {
    btn.removeAttribute("aria-busy");
  }
}

try { $("auto-read").checked = localStorage.getItem("autoRead") === "1"; } catch { /* storage blocked */ }
$("auto-read").addEventListener("change", (e) => {
  try { localStorage.setItem("autoRead", e.target.checked ? "1" : "0"); } catch { /* storage blocked */ }
  if (!e.target.checked) stopAudio();
});

/* ---------- Reset ---------- */
async function resetEverything() {
  stopPolling();
  resetChat();
  state.siteId = null;
  try { await post("/api/reset"); } catch { /* server unreachable; UI still resets */ }
  show("start");
  window.scrollTo(0, 0);
  $("url").select();
}
$("new-site").addEventListener("click", resetEverything);

/* ---------- Resume whatever the server is doing ---------- */
(async () => {
  try {
    const s = await (await fetch("/api/status")).json();
    applyLimits(s);
    if (s.stage === "ready") enterChat(s);
    else if (s.site_id) startPolling();
  } catch { /* stay on start screen */ }
})();
