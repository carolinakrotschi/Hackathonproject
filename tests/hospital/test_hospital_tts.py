import io

from hospital import tts

from klinikum import API


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_without_key_pager_falls_back(client, monkeypatch, tmp_path):
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    monkeypatch.setattr(tts, "KEY_FILE", tmp_path / "missing.env")
    assert client.get(f"{API}/tts", params={"text": "Hallo"}).status_code == 503


def test_team_key_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    key_file = tmp_path / "voice.env"
    key_file.write_text("# comment\nFISH_AUDIO_API_KEY=sk-team\n", encoding="utf-8")
    monkeypatch.setattr(tts, "KEY_FILE", key_file)
    assert tts.api_key() == "sk-team"
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "sk-own")
    assert tts.api_key() == "sk-own"


def test_audio_is_cached(client, monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-key")
    monkeypatch.setattr(tts, "_cache", type(tts._cache)())
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        return FakeResponse(b"ID3fake")

    monkeypatch.setattr(tts.urllib.request, "urlopen", fake_urlopen)
    for _ in range(2):
        response = client.get(f"{API}/tts", params={"text": "Please set off now."})
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg" and response.content == b"ID3fake"
    assert len(calls) == 1
    assert calls[0].get_header("Authorization") == "Bearer test-key"


def test_rejects_overlong_text(client):
    assert client.get(f"{API}/tts", params={"text": "x" * (tts.MAX_CHARS + 1)}).status_code == 422
