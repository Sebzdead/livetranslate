"""Control-panel HTTP server. Binds loopback ONLY — it reads/writes API keys.

Same stdlib idiom as display/server.py: ThreadingHTTPServer + a handler class
specialised per instance. JSON API + one static page.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tomlkit

from . import files, netinfo
from .audio_probe import LevelMeter, list_input_devices
from .process import PipelineProcess

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

# adapter name -> env var it requires (translate key is always required)
ADAPTER_KEYS = {"elevenlabs": "ELEVENLABS_API_KEY", "assemblyai": "ASSEMBLYAI_API_KEY"}


class ControlState:
    """Paths + pipeline handle + (optional) running level meter."""

    def __init__(self, root):
        self.root = Path(root)
        self.config_path = self.root / "config.toml"
        self.env_path = self.root / ".env"
        self.pipeline = PipelineProcess(self.root)
        self.meter = None
        self.meter_lock = threading.Lock()

    def config_doc(self):
        return tomlkit.parse(files.read_config_text(self.config_path))

    def glossary_path(self) -> Path:
        return self.root / str(self.config_doc()["glossary"]["path"])

    def start_meter(self, device_index: int) -> None:
        with self.meter_lock:
            if self.meter is not None:
                self.meter.stop()
            meter = LevelMeter(device_index)
            meter.start()
            self.meter = meter

    def stop_meter(self) -> None:
        with self.meter_lock:
            if self.meter is not None:
                self.meter.stop()
                self.meter = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: ControlState = None

    def log_message(self, fmt, *args):
        log.debug("control http: " + fmt, *args)

    # ---------- plumbing ----------

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _static(self, name, ctype):
        p = STATIC / name
        if not p.exists():
            self._json(500, {"error": f"static file not yet built: {name}"})
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- routing ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
            elif parsed.path == "/api/state":
                self._json(200, self._state_payload())
            elif parsed.path == "/api/config":
                self._json(200, {"text": files.read_config_text(self.state.config_path)})
            elif parsed.path == "/api/glossary":
                self._json(200, {"text": self.state.glossary_path().read_text(encoding="utf-8")})
            elif parsed.path == "/api/audio/devices":
                substring = str(self.state.config_doc()["audio"]["device_substring"])
                self._json(200, {"devices": list_input_devices(substring)})
            elif parsed.path == "/api/meter":
                with self.state.meter_lock:
                    meter = self.state.meter
                if meter is None:
                    self._json(409, {"error": "meter not running"})
                else:
                    self._json(200, meter.read())
            elif parsed.path == "/api/logs":
                after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                lines, cursor = self.state.pipeline.logs_since(after)
                self._json(200, {"lines": lines, "cursor": cursor})
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            log.exception("GET %s failed", parsed.path)
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        payload = self._body()
        if payload is None:
            self._json(400, {"error": "invalid JSON body"})
            return
        try:
            if parsed.path == "/api/config":
                self._post_config(payload)
            elif parsed.path == "/api/glossary":
                self._post_glossary(payload)
            elif parsed.path == "/api/keys":
                updates = {k: v for k, v in payload.items() if k in files.SECRET_KEYS}
                files.write_env_keys(self.state.env_path, updates)
                self._json(200, {"ok": True})
            elif parsed.path == "/api/meter":
                if self.state.pipeline.running():
                    self._json(409, {"error": "pipeline running; meter unavailable"})
                else:
                    self.state.start_meter(int(payload["device_index"]))
                    self._json(200, {"ok": True})
            elif parsed.path == "/api/meter/stop":
                self.state.stop_meter()
                self._json(200, {"ok": True})
            elif parsed.path == "/api/server/start":
                self._post_start()
            elif parsed.path == "/api/server/stop":
                self.state.pipeline.stop()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            log.exception("POST %s failed", parsed.path)
            self._json(500, {"error": str(exc)})

    # ---------- handlers ----------

    def _state_payload(self):
        doc = self.state.config_doc()
        ip = netinfo.lan_ip()
        env = files.read_env(self.state.env_path)
        pipe = self.state.pipeline
        return {
            "running": pipe.running(),
            "pid": pipe.proc.pid if pipe.running() else None,
            "started_at": pipe.started_at,
            "last_exit": pipe.last_exit,
            "lan_ip": ip,
            "display_port": int(doc["display"]["port"]),
            "links": netinfo.links(ip, int(doc["display"]["port"]),
                                   [str(t) for t in doc["translate"]["targets"]]),
            "keys": [{"name": k, "set": k in env, "masked": files.mask(env.get(k, ""))}
                     for k in files.SECRET_KEYS],
            "meter_running": self.state.meter is not None,
        }

    def _post_config(self, payload):
        try:
            if "fields" in payload:
                files.update_config_fields(self.state.config_path, payload["fields"])
            else:
                files.write_config_text(self.state.config_path, payload.get("text", ""))
            self._json(200, {"ok": True})
        except ValueError as exc:
            self._json(400, {"problems": str(exc).split("; ")})

    def _post_glossary(self, payload):
        text = payload.get("text", "")
        result = files.validate_glossary_text(text)
        if result["problems"]:
            self._json(400, result)
            return
        files.write_glossary_text(self.state.glossary_path(), text)
        self._json(200, result)

    def _post_start(self):
        if self.state.pipeline.running():
            self._json(409, {"error": "pipeline already running"})
            return
        doc = self.state.config_doc()
        env = files.read_env(self.state.env_path)
        required = ["TRANSLATE_API_KEY", ADAPTER_KEYS[str(doc["asr"]["adapter"])]]
        if doc["asr"].get("failover"):
            required.append(ADAPTER_KEYS[str(doc["asr"]["failover"])])
        missing = [k for k in required if not env.get(k)]
        if missing:
            self._json(400, {"error": "missing API keys: " + ", ".join(missing)})
            return
        self.state.stop_meter()       # never hold the device open under the pipeline
        self.state.pipeline.start(extra_env=env)
        self._json(200, {"ok": True})


class ControlServer:
    def __init__(self, state: ControlState, host="127.0.0.1", port=8766):
        handler = type("Handler", (_Handler,), {"state": state})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="control-http", daemon=False)

    def start(self):
        self._thread.start()

    def join(self):
        self._thread.join()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
