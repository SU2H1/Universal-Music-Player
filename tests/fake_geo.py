"""A fake IP geolocation service for the tests (2.4): tests must never ask the real one.

`FakeGeo` is a threaded HTTP server on 127.0.0.1 that answers every GET like
FreeIPAPI's free API (``/api/v1/json/``) with an invented place, records each
request's path and headers, and can be told to answer with an error status
(and a Retry-After) instead.  `ANSWER` is the invented place.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional, Tuple

# shaped like FreeIPAPI's answer (the place is invented, the address is a documentation one)
ANSWER = {"ipVersion": 4, "ipAddress": "198.51.100.7", "latitude": 35.68951, "longitude": 139.69171,
          "countryName": "Japan", "countryCode": "JP", "capital": "Tokyo", "phoneCodes": [81],
          "timeZones": ["Asia/Tokyo"], "zipCode": "160-0023", "cityName": "Shinjuku", "regionName": "Tokyo",
          "regionCode": "13", "continent": "Asia", "continentCode": "AS", "currencies": ["JPY"],
          "languages": ["ja"], "asn": "64500", "asnOrganization": "Example Net", "isProxy": False}


class FakeGeo:
    def __init__(self, answer: Optional[dict] = None):
        self.answer = dict(answer or ANSWER)
        self.requests: List[Tuple[str, dict]] = []
        self.status: Optional[Tuple[int, dict]] = None  # (status, headers) to answer instead
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                fake.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if fake.status is not None:
                    code, headers = fake.status
                    self.send_response(code)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    return
                body = json.dumps(fake.answer).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/json/"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
