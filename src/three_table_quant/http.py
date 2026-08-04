from __future__ import annotations

import time
import urllib.error
import urllib.request

try:
    import certifi
except ImportError:  # Tests can remain stdlib-only; package installs certifi in production.
    certifi = None


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, timeout: float = 20.0, attempts: int = 3) -> None:
        self.timeout = timeout
        self.attempts = attempts

    def get_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "three-table-quant/0.1 (+https://github.com/njedu2023-prog/A)",
                "Accept": "*/*",
            },
        )
        last_error: Exception | None = None
        context = None
        if certifi is not None:
            import ssl

            context = ssl.create_default_context(cafile=certifi.where())
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    time.sleep(0.5 * (2**attempt))
        raise HttpError(f"GET failed after {self.attempts} attempts: {url}: {last_error}")

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8-sig")
