from dataclasses import dataclass


@dataclass(frozen=True)
class AudioChunk:
    pcm16: bytes            # mono, little-endian
    sample_rate: int        # 16000
    t_start_ms: int         # position on the session stream timeline
    duration_ms: int
    seq: int


@dataclass(frozen=True)
class TranscriptEvent:
    kind: str               # "partial" | "final"
    text: str
    t_audio_start_ms: int
    t_audio_end_ms: int
    vendor: str             # "elevenlabs" | "assemblyai"
    t_received_wall: float
    vendor_raw: dict


@dataclass(frozen=True)
class Sentence:
    sid: int                # monotonic, gapless per session
    text: str
    t_audio_start_ms: int
    t_audio_end_ms: int
    t_finalized_wall: float
    paragraph_break: bool = False


@dataclass(frozen=True)
class Translation:
    sid: int
    lang: str
    text: str
    status: str             # "ok" | "failed"
    t_done_wall: float
    model: str
    attempt: int


@dataclass
class StatusEvent:
    level: str              # "info" | "warn" | "error"
    source: str             # "asr" | "translate.es" | "watchdog" | ...
    message: str
    t_wall: float
