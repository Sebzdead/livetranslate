import os
import pytest

@pytest.fixture(autouse=True, scope="session")
def _dummy_api_keys():
    """Tests never make real network calls (transport is always injected/mocked),
    but provider request-mappers read these env vars to build headers. Set dummy
    values so tests are hermetic and don't depend on the ambient environment."""
    for var in ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY"):
        os.environ.setdefault(var, "test-dummy-key")
    yield
