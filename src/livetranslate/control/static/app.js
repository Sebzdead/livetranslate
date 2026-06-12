/* Control panel client: thin polling layer over the JSON API. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function api(path, payload) {
  const opts = payload === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || (body.problems || []).join("; ") || resp.status);
  return body;
}

function setMsg(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
  if (ok) setTimeout(() => { el.textContent = ""; }, 4000);
}

/* ---------- state poll ---------- */
let running = false, logCursor = 0, metering = false, meterGen = 0;

async function refreshState() {
  const st = await api("/api/state");
  running = st.running;
  const pill = $("status");
  if (st.running) { pill.textContent = "running"; pill.className = "pill ok"; }
  else if (st.last_exit !== null && st.last_exit !== 0) {
    pill.textContent = "exited (" + st.last_exit + ")"; pill.className = "pill err";
  } else { pill.textContent = "stopped"; pill.className = "pill off"; }

  $("btn-start").disabled = st.running;
  $("btn-stop").disabled = !st.running;
  $("launch-info").textContent = st.running ? "pid " + st.pid : "";
  $("lan-ip").textContent = st.lan_ip;

  setGlossaryTargets(st.links.languages.map((l) => l.lang));

  const rows = [["Operator console", st.links.operator]].concat(
    st.links.languages.map((l) => [l.name + " (" + l.lang + ")", l.url]));
  $("links").innerHTML = rows.map(([name, url]) =>
    `<tr><td>${name}</td><td><a href="${url}" target="_blank">${url}</a></td>
     <td><button class="copy" data-url="${url}">copy</button></td></tr>`).join("");
  document.querySelectorAll(".copy").forEach((b) =>
    b.addEventListener("click", () => navigator.clipboard.writeText(b.dataset.url)));

  const consoleCard = $("console-card");
  if (st.running && consoleCard.style.display === "none") {
    consoleCard.style.display = "";
    $("console").src = "http://" + location.hostname + ":" + st.display_port + "/";
  } else if (!st.running) {
    consoleCard.style.display = "none";
    $("console").src = "about:blank";
  }

  const typing = document.activeElement && document.activeElement.dataset &&
                 document.activeElement.dataset.key;
  if (!typing) {
    $("keys").innerHTML = st.keys.map((k) => `
      <label>${k.name} ${k.set ? "(saved " + k.masked + ")" : "(not set)"}</label>
      <input type="password" data-key="${k.name}"
             placeholder="${k.set ? "leave blank to keep current" : "paste key"}">`).join("");
  }
}

/* ---------- logs ---------- */
async function pollLogs() {
  const body = await api("/api/logs?after=" + logCursor);
  if (body.lines.length) {
    logCursor = body.cursor;
    const el = $("logs");
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    el.textContent += body.lines.join("\n") + "\n";
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}

/* ---------- audio ---------- */
async function refreshDevices() {
  const body = await api("/api/audio/devices");
  $("devices").innerHTML = body.devices.map((d) =>
    `<option value="${d.index}" ${d.matches ? "selected" : ""}>
       ${esc(d.name)}${d.matches ? " ✓ matches config" : ""}</option>`).join("");
}

function stopMeterUi() {
  metering = false;
  meterGen++;
  $("btn-meter").textContent = "Test level";
  $("meterbar").style.width = "0%";
  $("meter-num").textContent = "";
}

async function toggleMeter() {
  if (metering) {
    await api("/api/meter/stop", {});
    stopMeterUi();
    return;
  }
  try {
    await api("/api/meter", { device_index: parseInt($("devices").value, 10) });
    metering = true;
    meterGen++;
    $("btn-meter").textContent = "Stop test";
    pollMeter(meterGen);
  } catch (e) { setMsg("audio-msg", e.message, false); }
}

async function pollMeter(gen) {
  if (gen !== meterGen) return;            // stale loop exits
  try {
    const r = await api("/api/meter");
    if (gen !== meterGen) return;          // re-check after await
    const pct = Math.max(0, Math.min(100, (r.rms_dbfs + 60) / 60 * 100));
    $("meterbar").style.width = pct + "%";
    $("meter-num").textContent = r.rms_dbfs + " / " + r.peak_dbfs + " dBFS";
  } catch (e) { stopMeterUi(); return; }   // meter stopped server-side
  setTimeout(() => pollMeter(gen), 150);
}

/* ---------- config ---------- */
function parseTomlValue(text, section, key) {
  // crude single-value extraction for prefilling the form; raw editor is authoritative
  const sec = text.split("[" + section + "]")[1] || "";
  const m = sec.split("[")[0].match(new RegExp('(?:^|\\n)\\s*' + key + '\\s*=\\s*"?([^"\\n#]*)"?'));
  return m ? m[1].trim() : "";
}

function setSelect(id, value) {
  const sel = $(id);
  if (value && ![...sel.options].some((o) => o.value === value)) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    sel.appendChild(opt);
  }
  sel.value = value;
}

async function refreshConfig() {
  const body = await api("/api/config");
  $("cfg-raw").value = body.text;
  $("cfg-device").value = parseTomlValue(body.text, "audio", "device_substring");
  setSelect("cfg-srclang", parseTomlValue(body.text, "session", "source_language"));
  setSelect("cfg-adapter", parseTomlValue(body.text, "asr", "adapter"));
  const targets = (body.text.match(/targets\s*=\s*\[([^\]]*)\]/) || [, ""])[1];
  $("cfg-targets").value = targets.replace(/["\s]/g, "");
}

async function saveConfigFields() {
  const targets = $("cfg-targets").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    await api("/api/config", { fields: {
      "audio.device_substring": $("cfg-device").value,
      "session.source_language": $("cfg-srclang").value,
      "asr.adapter": $("cfg-adapter").value,
      "translate.targets": targets,
    }});
    setMsg("cfg-msg", "Saved.", true);
    await refreshConfig(); await refreshState(); await refreshDevices();
  } catch (e) { setMsg("cfg-msg", e.message, false); }
}

async function saveConfigRaw() {
  try {
    await api("/api/config", { text: $("cfg-raw").value });
    setMsg("cfg-msg", "Saved raw TOML.", true);
    await refreshConfig(); await refreshState(); await refreshDevices();
  } catch (e) { setMsg("cfg-msg", e.message, false); }
}

/* ---------- glossary ---------- */
const GLOSSARY_COLS = ["term_src", "es", "fr", "de", "pt", "ar", "zh",
                       "priority", "notes"];
let glossaryRows = [];                      // [{col: value}] — single source of truth
let glossaryTargets = [];                   // lang codes shown as columns

const escAttr = (s) => esc(s).replace(/"/g, "&quot;");

function parseGlossary(text) {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
  if (!lines.length) return [];
  const header = lines[0].replace(/^﻿/, "").split("\t");
  return lines.slice(1).map((line) => {
    const cells = line.split("\t");
    const row = {};
    GLOSSARY_COLS.forEach((c) => {
      const i = header.indexOf(c);
      row[c] = i >= 0 && i < cells.length ? cells[i] : "";
    });
    return row;
  });
}

function serializeGlossary() {
  return [GLOSSARY_COLS.join("\t")]
    .concat(glossaryRows.map((r) => GLOSSARY_COLS.map((c) => r[c] || "").join("\t")))
    .join("\n") + "\n";
}

function renderGlossary() {
  const cols = ["term_src"].concat(glossaryTargets, ["priority", "notes"]);
  const cls = (c) => c === "priority" ? ' class="num"' : "";
  const head = "<tr>" + cols.map((c) =>
    `<th${cls(c)}>${c === "term_src" ? "term (source)" : c}</th>`).join("")
    + '<th class="del"></th></tr>';
  const body = glossaryRows.map((row, i) => "<tr>" + cols.map((c) =>
    `<td${cls(c)}><input data-row="${i}" data-col="${c}"
        value="${escAttr(row[c] || "")}"
        ${c === "term_src" ? 'placeholder="…"' : ""}></td>`).join("")
    + `<td class="del"><button class="delbtn" data-del="${i}"
         title="delete row">✗</button></td></tr>`).join("");
  $("glossary-table").innerHTML = head + body;
  document.querySelectorAll("#glossary-table input").forEach((inp) =>
    inp.addEventListener("input", () => {
      glossaryRows[+inp.dataset.row][inp.dataset.col] = inp.value;
    }));
  document.querySelectorAll("#glossary-table .delbtn").forEach((b) =>
    b.addEventListener("click", () => {
      glossaryRows.splice(+b.dataset.del, 1);
      renderGlossary();
    }));
  $("glossary").value = serializeGlossary();
}

function setGlossaryTargets(langs) {
  if (JSON.stringify(langs) === JSON.stringify(glossaryTargets)) return;
  glossaryTargets = langs;
  renderGlossary();                          // column set changed
}

async function refreshGlossary() {
  const body = await api("/api/glossary");
  glossaryRows = parseGlossary(body.text);
  renderGlossary();
}

async function saveGlossary() {
  try {
    const r = await api("/api/glossary", { text: serializeGlossary() });
    $("glossary-counts").textContent =
      r.terms + " terms; " + r.keyterms + " keyterms sent to ASR (cap 50)";
    setMsg("glossary-msg", "Saved.", true);
  } catch (e) { setMsg("glossary-msg", e.message, false); }
}

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1]);
    r.onerror = () => reject(new Error("could not read file"));
    r.readAsDataURL(file);
  });
}

async function generateGlossary() {
  const f = $("notes-file").files[0];
  if (!f) { setMsg("glossary-msg", "choose a notes file first (.pdf or .txt)", false); return; }
  const btn = $("btn-generate");
  btn.disabled = true;
  const msg = $("glossary-msg");
  msg.className = "msg";
  msg.textContent = "consulting DeepSeek — this can take a minute…";
  try {
    const content_b64 = await fileToB64(f);
    const r = await api("/api/glossary/generate",
                        { filename: f.name, content_b64 });
    glossaryRows = parseGlossary(r.text);
    renderGlossary();
    $("glossary-counts").textContent = "+" + r.added + " drafted, " + r.skipped
      + " already present — unsaved; review and save";
    setMsg("glossary-msg", "Drafted " + r.added + " terms from " + f.name + ".", true);
  } catch (e) { setMsg("glossary-msg", e.message, false); }
  btn.disabled = false;
}

/* ---------- keys ---------- */
async function saveKeys() {
  const updates = {};
  document.querySelectorAll("#keys input").forEach((i) => {
    if (i.value) updates[i.dataset.key] = i.value;
  });
  try {
    await api("/api/keys", updates);
    setMsg("keys-msg", "Saved.", true);
    await refreshState();
  } catch (e) { setMsg("keys-msg", e.message, false); }
}

/* ---------- launch ---------- */
async function startServer() {
  try {
    await api("/api/server/start", {});
    stopMeterUi();
    setMsg("launch-msg", "Pipeline starting — watch the log below.", true);
  } catch (e) { setMsg("launch-msg", e.message, false); }
  await refreshState();
}

async function stopServer() {
  try { await api("/api/server/stop", {}); setMsg("launch-msg", "Stopped.", true); }
  catch (e) { setMsg("launch-msg", e.message, false); }
  await refreshState();
}

/* ---------- wiring ---------- */
$("btn-start").addEventListener("click", startServer);
$("btn-stop").addEventListener("click", stopServer);
$("btn-meter").addEventListener("click", toggleMeter);
$("btn-save-cfg").addEventListener("click", saveConfigFields);
$("btn-save-raw").addEventListener("click", saveConfigRaw);
$("btn-save-glossary").addEventListener("click", saveGlossary);
$("btn-generate").addEventListener("click", generateGlossary);
$("btn-add-term").addEventListener("click", () => {
  const row = {};
  GLOSSARY_COLS.forEach((c) => { row[c] = ""; });
  row.priority = "2";
  glossaryRows.push(row);
  renderGlossary();
  const inputs = document.querySelectorAll('#glossary-table input[data-col="term_src"]');
  if (inputs.length) inputs[inputs.length - 1].focus();
});
$("btn-raw-apply").addEventListener("click", () => {
  glossaryRows = parseGlossary($("glossary").value);
  renderGlossary();
  setMsg("glossary-msg", "Raw TSV loaded into table (unsaved).", true);
});
$("btn-save-keys").addEventListener("click", saveKeys);

(async function init() {
  await refreshState();
  await refreshConfig();
  await refreshGlossary();
  await refreshDevices().catch((e) => setMsg("audio-msg", e.message, false));
  setInterval(refreshState, 2000);
  setInterval(() => { if (running || logCursor > 0) pollLogs().catch(() => {}); }, 1000);
})();
