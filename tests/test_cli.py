import subprocess, sys

def test_help_runs():
    r = subprocess.run([sys.executable, "-m", "livetranslate", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "--config" in r.stdout and "--resume" in r.stdout

def test_no_secrets_logged():
    from livetranslate.logging_setup import SecretRedactingFilter
    import logging
    f = SecretRedactingFilter(["sk-supersecret"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key is sk-supersecret ok", (), None)
    f.filter(rec)
    assert "sk-supersecret" not in rec.getMessage()
