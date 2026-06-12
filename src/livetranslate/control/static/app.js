/* Control panel client: thin polling layer over the JSON API. */
const $ = (id) => document.getElementById(id);

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
let running = false, logCursor = 0, metering = false;

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
       ${d.name}${d.matches ? " ✓ matches config" : ""}</option>`).join("");
}

async function toggleMeter() {
  if (metering) {
    await api("/api/meter/stop", {});
    metering = false;
    $("btn-meter").textContent = "Test level";
    $("meterbar").style.width = "0%";
    $("meter-num").textContent = "";
    return;
  }
  try {
    await api("/api/meter", { device_index: parseInt($("devices").value, 10) });
    metering = true;
    $("btn-meter").textContent = "Stop test";
    pollMeter();
  } catch (e) { setMsg("audio-msg", e.message, false); }
}

async function pollMeter() {
  if (!metering) return;
  try {
    const r = await api("/api/meter");
    const pct = Math.max(0, Math.min(100, (r.rms_dbfs + 60) / 60 * 100));
    $("meterbar").style.width = pct + "%";
    $("meter-num").textContent = r.rms_dbfs + " / " + r.peak_dbfs + " dBFS";
  } catch (e) { /* meter stopped server-side */ metering = false; }
  setTimeout(pollMeter, 150);
}

/* ---------- config ---------- */
function parseTomlValue(text, section, key) {
  // crude single-value extraction for prefilling the form; raw editor is authoritative
  const sec = text.split("[" + section + "]")[1] || "";
  const m = sec.split("[")[0].match(new RegExp(key + '\\s*=\\s*"?([^"\\n#]*)"?'));
  return m ? m[1].trim() : "";
}

async function refreshConfig() {
  const body = await api("/api/config");
  $("cfg-raw").value = body.text;
  $("cfg-device").value = parseTomlValue(body.text, "audio", "device_substring");
  $("cfg-srclang").value = parseTomlValue(body.text, "session", "source_language");
  $("cfg-adapter").value = parseTomlValue(body.text, "asr", "adapter");
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
async function refreshGlossary() {
  const body = await api("/api/glossary");
  $("glossary").value = body.text;
}

async function saveGlossary() {
  try {
    const r = await api("/api/glossary", { text: $("glossary").value });
    $("glossary-counts").textContent =
      r.terms + " terms; " + r.keyterms + " keyterms sent to ASR (cap 50)";
    setMsg("glossary-msg", "Saved.", true);
  } catch (e) { setMsg("glossary-msg", e.message, false); }
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
    metering = false; $("btn-meter").textContent = "Test level";
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
$("btn-save-keys").addEventListener("click", saveKeys);

(async function init() {
  await refreshState();
  await refreshConfig();
  await refreshGlossary();
  await refreshDevices().catch((e) => setMsg("audio-msg", e.message, false));
  setInterval(refreshState, 2000);
  setInterval(() => { if (running || logCursor > 0) pollLogs().catch(() => {}); }, 1000);
})();
