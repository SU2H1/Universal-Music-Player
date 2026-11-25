"""packaging/fetch_ffmpeg.py: retrying after HTTP 429 and server errors, and the macOS
Intel fallback to the latest release's copy of evermeet.cx's builds (2.4: evermeet.cx
answered 429 to GitHub's macOS runners for half an hour).  Offline: a local server."""

import http.server
import io
import sys
import threading
import urllib.error
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import fetch_ffmpeg as ff  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    """A local server answering from `answers` (path -> list of (status, body)), recording requests."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    answers, hits = {}, []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            queue = answers.get(self.path) or [(404, b"")]
            status, body = queue.pop(0) if len(queue) > 1 else queue[0]
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", answers, hits
    srv.shutdown()


def test_download_tries_again_after_429_and_server_errors(server, monkeypatch):
    base, answers, hits = server
    monkeypatch.setattr(ff, "_wait_before_retry", lambda *a: 0.0)
    answers["/x"] = [(429, b""), (503, b""), (200, b"ok")]
    assert ff.download(base + "/x") == b"ok" and hits == ["/x"] * 3
    hits.clear()
    with pytest.raises(urllib.error.HTTPError):  # not found: at once
        ff.download(base + "/missing")
    assert hits == ["/missing"]
    hits.clear()
    answers["/busy"] = [(429, b"")]
    with pytest.raises(urllib.error.HTTPError):  # still refused after its attempts
        ff.download(base + "/busy", attempts=2)
    assert hits == ["/busy"] * 2


def test_retry_after_is_honoured_up_to_the_limit():
    class Refused(Exception):
        def __init__(self, value):
            self.headers = {"Retry-After": value}

    assert ff._wait_before_retry(Refused("40"), 0) == 40
    assert ff._wait_before_retry(Refused("9999"), 0) == ff.MAX_WAIT_S
    assert ff._wait_before_retry(Refused("9999"), 0, 60) == 60
    assert ff._wait_before_retry(Exception(), 2) == 60  # 15, 30, 60...


def test_the_release_asset_is_named_after_the_latest_tag():
    url = ff.released_asset("https://github.com/SU2H1/Universal-Music-Player/releases/tag/v2.3.0")
    assert url == ("https://github.com/SU2H1/Universal-Music-Player/releases/download/v2.3.0/"
                   "UniversalMusicPlayer-Rust-2.3.0-macos-x86_64.zip")
    with pytest.raises(ValueError):
        ff.released_asset("https://github.com/SU2H1/Universal-Music-Player/releases")


def test_evermeet_refusing_falls_back_to_the_release(server, monkeypatch, tmp_path):
    base, answers, hits = server
    monkeypatch.setattr(ff, "_wait_before_retry", lambda *a: 0.0)
    monkeypatch.setattr(ff, "EVERMEET_INFO", base + "/info/{tool}/release")
    monkeypatch.setattr(ff, "LATEST_RELEASE", base + "/{repo}/releases/latest")
    monkeypatch.setattr(ff, "RELEASE_ASSET", base + "/{repo}/download/{tag}/pkg-{version}.zip")
    # evermeet.cx: the info answers, the zip is refused
    answers["/info/ffmpeg/release"] = [(200, ('{"download": {"zip": {"url": "%s/ffmpeg.zip"}}}' % base).encode())]
    answers["/ffmpeg.zip"] = [(429, b"")]
    # the latest release: a redirect to its tag, then the package with both programs
    pkg = io.BytesIO()
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("UniversalMusicPlayer-Rust/UniversalMusicPlayer-Rust.app/Contents/MacOS/ffmpeg", b"FF")
        z.writestr("UniversalMusicPlayer-Rust/UniversalMusicPlayer-Rust.app/Contents/MacOS/ffprobe", b"FP")
    repo = ff.REPO
    answers[f"/{repo}/releases/tag/v9.9.9"] = [(200, b"release page")]
    answers[f"/{repo}/download/v9.9.9/pkg-9.9.9.zip"] = [(200, pkg.getvalue())]

    real = ff.download

    def download(url, *a, **kw):
        if url.endswith("/releases/latest"):  # what github.com answers: a redirect to the tag
            kw.get("final_url", []).append(f"https://github.com/{repo}/releases/tag/v9.9.9")
            return b""
        return real(url, *a, **kw)

    monkeypatch.setattr(ff, "download", download)
    ff.fetch_evermeet_macos(str(tmp_path))
    assert (tmp_path / "ffmpeg").read_bytes() == b"FF" and (tmp_path / "ffprobe").read_bytes() == b"FP"
    assert hits.count("/ffmpeg.zip") == ff.EVERMEET_ATTEMPTS  # it tried evermeet.cx first
    assert f"/{repo}/download/v9.9.9/pkg-9.9.9.zip" in hits
