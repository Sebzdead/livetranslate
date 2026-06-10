import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .types import Sentence, Translation

log = logging.getLogger(__name__)

LANG_NAMES = {"es": "Spanish", "fr": "French", "de": "German",
              "pt": "Portuguese", "ar": "Arabic", "zh": "Chinese"}

SYSTEM_TEMPLATE = """You are a professional simultaneous interpreter producing written captions for a live conference.
Translate the SOURCE sentence into {lang_name}.
Rules:
- Output ONLY the translation. No quotes, no notes, no commentary.
- Register: natural spoken-presentation {lang_name}; faithful to meaning; do not summarize or embellish.
- Keep numbers, units, and personal names exact.
- Apply this glossary strictly (source term → required rendering; identical rendering means keep the term untranslated):
{glossary_block}
- CONTEXT lines are for cohesion only. Translate ONLY the SOURCE line.
{domain_blurb_line}"""

@dataclass
class TransContext:
    prev_source: list = field(default_factory=list)   # previous 2 source sentences
    prev_target: str = ""                              # previous output in this lang
    glossary_block: str = ""
    domain_blurb: str = ""

    @classmethod
    def empty(cls, glossary_block: str = "") -> "TransContext":
        return cls(glossary_block=glossary_block)

def build_messages(sentence: Sentence, lang: str, lang_name: str,
                   ctx: TransContext) -> list:
    blurb_line = f"Subject of the event: {ctx.domain_blurb}" if ctx.domain_blurb else ""
    system = SYSTEM_TEMPLATE.format(lang_name=lang_name,
                                    glossary_block=ctx.glossary_block,
                                    domain_blurb_line=blurb_line)
    user_lines = []
    if ctx.prev_source:
        user_lines.append("CONTEXT (source): " + " ".join(ctx.prev_source[-2:]))
    if ctx.prev_target:
        user_lines.append("CONTEXT (your previous output): " + ctx.prev_target)
    user_lines.append("SOURCE: " + sentence.text)
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user_lines)}]

# ---- provider request/response mappings (verified per §13) ----------
def _map_openai_chat(cfg, messages):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {os.environ[cfg['api_key_env']]}"}
    body = {"model": cfg["model"], "messages": messages, "temperature": 0}
    return url, headers, body

def _parse_openai_chat(resp_json):
    return resp_json["choices"][0]["message"]["content"].strip()

def _map_anthropic(cfg, messages):
    url = cfg["base_url"].rstrip("/") + "/v1/messages"
    headers = {"x-api-key": os.environ[cfg["api_key_env"]],
               "anthropic-version": "2023-06-01"}
    system = next(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    body = {"model": cfg["model"], "system": system, "messages": rest,
            "max_tokens": 1024, "temperature": 0}
    return url, headers, body

def _parse_anthropic(resp_json):
    return resp_json["content"][0]["text"].strip()

PROVIDERS = {"openai_chat": (_map_openai_chat, _parse_openai_chat),
             "anthropic": (_map_anthropic, _parse_anthropic)}

def _default_post(url, headers, body, timeout_s):
    r = requests.post(url, headers=headers, json=body, timeout=timeout_s)
    r.raise_for_status()
    return {"ok": True, "json": r.json()}

class LLMTranslator:
    """Synchronous translator (spec §5.5). `post` is injectable for tests;
    in production it is the requests-based default."""

    MAX_ATTEMPTS = 3      # 1 try + 2 retries

    def __init__(self, cfg, post=None, backoff_s: float = 0.5, fallback_cfg=None):
        self.cfg, self.backoff_s = cfg, backoff_s
        self.fallback_cfg = fallback_cfg
        self._post = post or _default_post

    def _call(self, cfg, messages) -> str:
        mapper, parser = PROVIDERS[cfg["provider"]]
        url, headers, body = mapper(cfg, messages)
        resp = self._post(url, headers, body, cfg["timeout_s"])
        if "text" in resp:           # injected test transport returns text directly
            return resp["text"]
        return parser(resp["json"])

    def _attempt_loop(self, messages):
        """Returns (text, attempts_used, model) or raises last error."""
        last = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return self._call(self.cfg, messages), attempt, self.cfg["model"]
            except Exception as e:                       # noqa: BLE001
                last = e
                log.warning("translate attempt %d failed: %s", attempt, e)
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
        if self.fallback_cfg:
            try:
                return self._call(self.fallback_cfg, messages), self.MAX_ATTEMPTS, \
                       self.fallback_cfg["model"]
            except Exception as e:                       # noqa: BLE001
                last = e
        raise last

    def translate(self, sentence: Sentence, lang: str, ctx: TransContext) -> Translation:
        messages = build_messages(sentence, lang, LANG_NAMES[lang], ctx)
        try:
            text, attempts, model = self._attempt_loop(messages)
            return Translation(sid=sentence.sid, lang=lang, text=text, status="ok",
                               t_done_wall=time.monotonic(), model=model, attempt=attempts)
        except Exception:                                # noqa: BLE001 — terminal failure
            return Translation(sid=sentence.sid, lang=lang,
                               text="⟨translation unavailable⟩", status="failed",
                               t_done_wall=time.monotonic(), model=self.cfg["model"],
                               attempt=self.MAX_ATTEMPTS)

    def translate_batch(self, sentences, lang: str, ctx: TransContext):
        """Catch-up batching: numbered-list request; on parse mismatch fall
        back to per-sentence calls (spec §5.5)."""
        numbered = "\n".join(f"{i+1}. {s.text}" for i, s in enumerate(sentences))
        pseudo = Sentence(sid=sentences[0].sid,
                          text=("Translate each numbered sentence separately; "
                                "reply with the same numbered list.\n" + numbered),
                          t_audio_start_ms=sentences[0].t_audio_start_ms,
                          t_audio_end_ms=sentences[-1].t_audio_end_ms,
                          t_finalized_wall=sentences[0].t_finalized_wall)
        messages = build_messages(pseudo, lang, LANG_NAMES[lang], ctx)
        try:
            text, attempts, model = self._attempt_loop(messages)
            parts = _split_numbered(text, len(sentences))
            if parts is not None:
                now = time.monotonic()
                return [Translation(sid=s.sid, lang=lang, text=p, status="ok",
                                    t_done_wall=now, model=model, attempt=attempts)
                        for s, p in zip(sentences, parts)]
            log.warning("batch parse mismatch (%d expected); falling back per-sentence",
                        len(sentences))
        except Exception as e:                           # noqa: BLE001
            log.warning("batch call failed (%s); falling back per-sentence", e)
        return [self.translate(s, lang, ctx) for s in sentences]

def _split_numbered(text: str, n: int):
    items = re.findall(r"^\s*(\d+)[.)]\s*(.+?)(?=^\s*\d+[.)]|\Z)", text,
                       re.MULTILINE | re.DOTALL)
    if len(items) != n:
        return None
    return [body.strip() for _num, body in items]
