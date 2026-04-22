#!/usr/bin/env python3
"""Nahraje titulky k už dříve uploadovaným filmům.

Spouští se SEPARÁTNĚ od sync.yml — typicky následující den, kdy prehraj.to
stihne dozpracovat videa. Pro zpracovávaná videa (`Zpracovává se`) prehraj.to
odmítá subtitle upload.

Flow pro každý kandidát:
  1. V state/uploaded.json najít záznamy bez `subtitles_uploaded`.
  2. Zpárovat s backlog/sledujteto-films.jsonl (přes sledujteto_file_id),
     zjistit jestli má titulky vůbec (`has_subtitles` + `subtitle_lang`).
  3. Zavolat add-file-link znovu — získat fresh subtitle URL.
  4. Stáhnout VTT (~50 kB, rychlé).
  5. POST multipart na prehraj.to uploadSubtitles endpoint:
     files[]=vtt, video=<prehrajto_video_id>
  6. Zapsat subtitles_uploaded=True + timestamp do state.

Endpoint objeveno reverse-engineeringem přes Playwright 2026-04-22:
  POST https://prehraj.to/profil/nahrana-videa?do=uploadedVideoListing-uploadSubtitles

Spuštění:
    PREHRAJTO_EMAIL=... PREHRAJTO_PASSWORD=... \\
        python3 src/upload_subtitles.py [--count N]
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prehrajto_upload import login  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / "state" / "uploaded.json"
BACKLOG = REPO_ROOT / "backlog" / "sledujteto-films.jsonl"
LOG_PATH = REPO_ROOT / "state" / "subs.log"

SLEDUJTETO_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
SLEDUJTETO_REFERER = "https://www.sledujteto.cz/"

UPLOAD_ENDPOINT = "https://prehraj.to/profil/nahrana-videa?do=uploadedVideoListing-uploadSubtitles"


def log(msg: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict:
    return json.loads(STATE.read_text())


def save_state(state: dict) -> None:
    state["last_updated"] = now_iso()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def load_backlog_by_id() -> dict[int, dict]:
    out = {}
    for line in BACKLOG.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["sledujteto_file_id"]] = r
    return out


def fetch_subtitle_urls(upload_id: int, timeout: int = 15) -> list[dict] | None:
    """POST add-file-link, vrátí seznam titulků (může být prázdný / None)."""
    payload = json.dumps({"params": {"id": int(upload_id)}}).encode("utf-8")
    req = urllib.request.Request(
        "https://www.sledujteto.cz/services/add-file-link",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": SLEDUJTETO_UA,
            "Referer": SLEDUJTETO_REFERER,
            "requested-with-angularjs": "true",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            if data.get("error"):
                return None
            return data.get("subtitles") or []
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        log(f"  add-file-link error for upload_id={upload_id}: {e}")
        return None


def fetch_subtitle_bytes(url: str, timeout: int = 15) -> bytes | None:
    # Sledujteto vrací buď přes wrapper /file/subtitles/?file=... nebo přímou
    # URL. Obě fungují; preferujeme přímou (cacheovaná, nedává cookies).
    direct = url
    # Strip /file/subtitles/?file= wrapper if present
    marker = "/file/subtitles/?file="
    if marker in direct:
        direct = direct.split(marker, 1)[1]
    req = urllib.request.Request(
        direct,
        headers={"User-Agent": SLEDUJTETO_UA, "Referer": SLEDUJTETO_REFERER},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"  subtitle fetch error {direct}: {e}")
        return None


def upload_subtitle(session, video_id: int, filename: str, content: bytes) -> dict:
    """POST multipart. Vrací dict {ok, status, error?}. ok=True znamená
    přijato; False s error message znamená problém (např. video dosud
    zpracovávané)."""
    r = session.post(
        UPLOAD_ENDPOINT,
        files=[
            ("files[]", (filename, content, "text/vtt")),
            ("video", (None, str(video_id))),
        ],
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://prehraj.to/profil/nahrana-videa",
            "Origin": "https://prehraj.to",
        },
        timeout=60,
    )
    out = {"status": r.status_code, "ok": r.status_code == 200}
    if r.status_code != 200:
        out["error"] = r.text[:300]
    return out


def process_one(entry: dict, backlog_map: dict[int, dict], session) -> dict:
    """Vrací dict {status, reason?, count?} kde status in {'uploaded','skipped','failed'}."""
    upload_id = entry["sledujteto_file_id"]
    video_id = entry["prehrajto_video_id"]
    title = entry.get("title", "?")

    backlog_row = backlog_map.get(upload_id)
    if not backlog_row:
        return {"status": "skipped", "reason": "not in backlog"}
    if not backlog_row.get("has_subtitles"):
        return {"status": "skipped", "reason": "no subtitles available"}

    log(f"step=subtitle start upload_id={upload_id} video_id={video_id} title='{title}'")
    subs = fetch_subtitle_urls(upload_id)
    if not subs:
        return {"status": "failed", "reason": "add-file-link returned no subtitles"}

    uploaded_count = 0
    for idx, s in enumerate(subs):
        url = s.get("file") or s.get("path")
        if not url:
            continue
        label = s.get("label") or f"sub-{idx}.vtt"
        # Sanitize filename for prehraj.to
        filename = label if label.endswith((".vtt", ".srt")) else label + ".vtt"
        content = fetch_subtitle_bytes(url)
        if not content:
            log(f"  failed to fetch sub #{idx}: {url}")
            continue
        log(f"  uploading sub #{idx} ({len(content)} B) as '{filename}'")
        res = upload_subtitle(session, video_id, filename, content)
        if res["ok"]:
            uploaded_count += 1
            log(f"  ✓ uploaded sub #{idx}")
        else:
            log(f"  ✗ sub #{idx} failed: http={res['status']} {res.get('error','')[:200]}")

    if uploaded_count == 0:
        return {"status": "failed", "reason": "no subtitle POST succeeded"}
    return {"status": "uploaded", "count": uploaded_count}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0,
                    help="Max videos to process (0=all)")
    ap.add_argument("--only-film-id", type=int, default=0,
                    help="Testovací: jen tenhle sledujteto_file_id")
    args = ap.parse_args()

    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        log("ERROR: chybí PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD")
        return 2

    state = load_state()
    backlog_map = load_backlog_by_id()

    uploads = state.get("uploads", [])
    candidates = []
    for u in uploads:
        if u.get("subtitles_uploaded"):
            continue
        if args.only_film_id and u["sledujteto_file_id"] != args.only_film_id:
            continue
        candidates.append(u)

    log(f"step=batch-start candidates={len(candidates)} total_uploads={len(uploads)}")
    if not candidates:
        log("step=batch-end nothing to do")
        return 0

    log("step=login")
    session = login(email, password)
    log("step=login done")

    succeeded = failed = skipped = 0
    for i, entry in enumerate(candidates, 1):
        if args.count and i > args.count:
            break
        log(f"step=iteration {i}/{len(candidates) if not args.count else min(args.count, len(candidates))}")
        try:
            res = process_one(entry, backlog_map, session)
        except Exception as e:
            log(f"  crash: {e}")
            res = {"status": "failed", "reason": f"exception: {e}"}
        if res["status"] == "uploaded":
            succeeded += 1
            entry["subtitles_uploaded"] = True
            entry["subtitles_uploaded_at"] = now_iso()
            entry["subtitles_count"] = res.get("count", 0)
        elif res["status"] == "skipped":
            skipped += 1
            entry["subtitles_skipped"] = res.get("reason", "unknown")
        else:
            failed += 1
            entry.setdefault("subtitles_failures", []).append({
                "reason": res.get("reason", "unknown"),
                "failed_at": now_iso(),
            })
        save_state(state)
        time.sleep(0.5)  # gentle rate-limit

    log(f"step=batch-end succeeded={succeeded} failed={failed} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
