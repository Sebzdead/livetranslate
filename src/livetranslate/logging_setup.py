import logging
import os

class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        env_secrets = [os.environ.get(k, "") for k in
                       ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY")]
        self.secrets = [s for s in (secrets or []) + env_secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            if s in msg:
                msg = msg.replace(s, "***REDACTED***")
        record.msg, record.args = msg, ()
        return True

def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.addFilter(SecretRedactingFilter())
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        handlers=[h])
