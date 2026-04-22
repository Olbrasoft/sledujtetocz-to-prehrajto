#!/usr/bin/env python3
"""Resolvuje sledujteto.cz video_url pro daný upload_id.

Sledujteto podepisuje hash per-CDN-host: hash vydaný `www.sledujteto.cz` pro
data11 upload je na data11 odmítnutý jako `invalid-file`. Proto
`add-file-link` voláme přímo na hostitele, kde soubor leží.

Flow:
  1. Pokud známe `sledujteto_cdn` (`data10` / `data11` / `www`), voláme
     `POST https://<host>.sledujteto.cz/services/add-file-link` s
     `{"params":{"id":<upload_id>}}`.
  2. Vrácený `video_url` = `https://<host>.sledujteto.cz/player/index/sledujteto/<hash>`
     — podporuje HTTP Range, stahuje jako obyčejný MP4.

Spuštění:
    python3 resolve_sledujteto_cdn.py <upload_id> [cdn_host]
"""
import json
import sys
import time
import urllib.request
import urllib.error

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
)
REFERER = "https://www.sledujteto.cz/"


def _add_file_link(host: str, upload_id: int, timeout: int = 15) -> dict | None:
    """POST add-file-link on a specific sledujteto host. Returns full JSON or None."""
    url = f"https://{host}.sledujteto.cz/services/add-file-link"
    if host == "www":
        url = f"https://www.sledujteto.cz/services/add-file-link"
    payload = json.dumps({"params": {"id": int(upload_id)}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": UA,
            "Referer": REFERER,
            "requested-with-angularjs": "true",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
                if data.get("error"):
                    return None
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            return None
    return None


def resolve(upload_id: int, cdn_host: str | None = None) -> str | None:
    """Vrátí `video_url` (MP4 stream se podporou Range) nebo None.

    cdn_host: `data10` / `data11` / `www` / None. Pokud None, nejprve
    zeptáme `www` a z odpovědi zjistíme správný host, pak re-issue na něm.
    """
    if cdn_host and cdn_host != "www":
        data = _add_file_link(cdn_host, upload_id)
        if data and data.get("video_url"):
            return data["video_url"]
        # Fall back: may have been wrong host hint
    # Ask www to learn which CDN this file lives on
    data = _add_file_link("www", upload_id)
    if not data or not data.get("video_url"):
        return None
    video_url = data["video_url"]
    # Parse host from returned URL
    from urllib.parse import urlparse
    host = (urlparse(video_url).hostname or "").split(".")[0]
    if host in ("www", "") :
        return video_url
    # Re-issue on the real CDN host to get a valid-there hash
    data = _add_file_link(host, upload_id)
    if data and data.get("video_url"):
        return data["video_url"]
    return video_url  # best effort


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(f"Použití: {sys.argv[0]} <upload_id> [cdn_host]", file=sys.stderr)
        return 2
    upload_id = int(sys.argv[1])
    cdn_host = sys.argv[2] if len(sys.argv) == 3 else None
    print(f"[cdn] resolving upload_id={upload_id} cdn={cdn_host}", file=sys.stderr)
    resolved = resolve(upload_id, cdn_host)
    if not resolved:
        print(f"[cdn] FAILED — add-file-link nevrátil video_url", file=sys.stderr)
        return 1
    print(f"[cdn] resolved → {resolved}", file=sys.stderr)
    print(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
