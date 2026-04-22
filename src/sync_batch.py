#!/usr/bin/env python3
"""Sync batch — nahraje N filmů v jednom GitHub Actions runu.

Pro každý film: pick → resolve_sledujteto_cdn → curl download → upload →
save_state → cleanup. State + log se zapisují **po každém filmu**, takže
timeout/crash neztratí progress.

Spuštění:
    PREHRAJTO_EMAIL=... PREHRAJTO_PASSWORD=... \\
        python3 src/sync_batch.py [--count 5]

Selhání jednoho filmu (CDN, download, upload) batch nezastaví — film se
zaloguje do state.failed_attempts a pokračuje se dalším.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pick_next_film import (  # noqa: E402
    BACKLOG, STATE,
    load_backlog, load_state, pick_next, display_name,
)
from resolve_sledujteto_cdn import resolve as resolve_cdn  # noqa: E402
from prehrajto_upload import login, upload_video  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "state" / "sync.log"
TMP_DIR = Path("/tmp")
MIN_FILE_SIZE = 10_000_000  # 10 MB — cokoliv menšího je dead CDN response


def log(msg: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_state(state: dict) -> None:
    state["last_updated"] = now_iso()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def safe_filename(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def download(url: str, dest: Path, timeout_sec: int = 1800) -> int:
    """Curl download s --fail + speed-limit. Vrací size v bajtech, raise při chybě."""
    # Sledujteto data{N} CDN vyžaduje Range header — bez něj vrací 403.
    # `bytes=0-` říká "od začátku do konce", server odpoví 206 s plnou content-length.
    cmd = [
        "curl", "-fL", url,
        "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
        "-H", "Referer: https://www.sledujteto.cz/",
        "-H", "Range: bytes=0-",
        "--max-time", str(timeout_sec),
        "--speed-time", "60", "--speed-limit", "10000",
        "-s", "-S",
        "-o", str(dest),
    ]
    log(f"step=download curl {url}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit={proc.returncode} stderr={proc.stderr.strip()[:200]}")
    size = dest.stat().st_size
    if size < MIN_FILE_SIZE:
        raise RuntimeError(f"file too small: {size} B (< {MIN_FILE_SIZE})")
    return size


def process_one(film: dict, session, state: dict) -> bool:
    """Vrací True při úspěchu, False při chybě (pokračuje se dál)."""
    name = display_name(film)
    description = film.get("description") or ""
    upload_id = film["sledujteto_file_id"]
    cr_film_id = film.get("cr_film_id")
    cdn_host = film.get("sledujteto_cdn")
    t_film_start = time.monotonic()
    log(f"step=film start upload_id={upload_id} cr_film_id={cr_film_id} name='{name}'")

    # 1. CDN resolve via add-file-link
    t = time.monotonic()
    log(f"step=cdn-resolve upload_id={upload_id} cdn={cdn_host}")
    resolved = resolve_cdn(upload_id, cdn_host)
    cdn_sec = round(time.monotonic() - t, 1)
    if not resolved:
        log(f"step=cdn-resolve FAILED upload_id={upload_id} after {cdn_sec}s")
        record_failure(state, film, "cdn_resolve_failed", {"cdn_resolve_sec": cdn_sec})
        return False
    log(f"step=cdn-resolve done {cdn_sec}s → {resolved}")

    # 2. Download
    tmp_path = TMP_DIR / f"{safe_filename(name)}.mp4"
    t = time.monotonic()
    try:
        size = download(resolved, tmp_path)
        dl_sec = round(time.monotonic() - t, 1)
        mbps = round(size / 1_000_000 / max(dl_sec, 0.001), 1)
        log(f"step=download done upload_id={upload_id} size={size} "
            f"dur={dl_sec}s speed={mbps}MB/s")
    except Exception as e:
        dl_sec = round(time.monotonic() - t, 1)
        log(f"step=download FAILED upload_id={upload_id} after {dl_sec}s err={e}")
        if tmp_path.exists():
            tmp_path.unlink()
        record_failure(state, film, f"download_failed: {e}",
                       {"cdn_resolve_sec": cdn_sec, "download_sec": dl_sec})
        return False

    # 3. Upload
    t = time.monotonic()
    try:
        log(f"step=upload start upload_id={upload_id} size={size}")
        video_id = upload_video(
            session, tmp_path,
            display_name=name, description=description,
        )
        up_sec = round(time.monotonic() - t, 1)
        up_mbps = round(size / 1_000_000 / max(up_sec, 0.001), 1)
        log(f"step=upload done upload_id={upload_id} video_id={video_id} "
            f"dur={up_sec}s speed={up_mbps}MB/s")
    except Exception as e:
        up_sec = round(time.monotonic() - t, 1)
        log(f"step=upload FAILED upload_id={upload_id} after {up_sec}s err={e}")
        record_failure(state, film, f"upload_failed: {e}",
                       {"cdn_resolve_sec": cdn_sec, "download_sec": dl_sec, "upload_sec": up_sec})
        return False
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
            log(f"step=cleanup unlinked {tmp_path.name}")

    total_sec = round(time.monotonic() - t_film_start, 1)
    log(f"step=film done upload_id={upload_id} total={total_sec}s "
        f"(cdn={cdn_sec}s + download={dl_sec}s + upload={up_sec}s)")

    state.setdefault("uploads", []).append({
        "sledujteto_file_id": upload_id,
        "sledujteto_cdn": cdn_host,
        "cr_film_id": cr_film_id,
        "cr_slug": film.get("cr_slug"),
        "title": film["title"],
        "year": film.get("year"),
        "lang_class": film.get("lang_class"),
        "prehrajto_video_id": video_id,
        "prehrajto_slug_path": None,
        "uploaded_at": now_iso(),
        "status": "processing",
        "size_bytes": size,
        "timing": {
            "cdn_resolve_sec": cdn_sec,
            "download_sec": dl_sec,
            "upload_sec": up_sec,
            "total_sec": total_sec,
        },
    })
    save_state(state)
    log(f"step=state-saved upload_id={upload_id} (total uploads={len(state['uploads'])})")
    return True


def record_failure(
    state: dict,
    film: dict,
    reason: str,
    timing: dict | None = None,
) -> None:
    """Failed_attempts seznam — film NENÍ do moderated_out, jen do failures.
    Příští run ho zase zkusí (CDN může být dočasně down)."""
    entry = {
        "sledujteto_file_id": film["sledujteto_file_id"],
        "cr_film_id": film.get("cr_film_id"),
        "cr_slug": film.get("cr_slug"),
        "title": film["title"],
        "year": film.get("year"),
        "reason": reason,
        "failed_at": now_iso(),
    }
    if timing:
        entry["timing"] = timing
    state.setdefault("failed_attempts", []).append(entry)
    save_state(state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5, help="Kolik filmů nahrát v této dávce")
    args = ap.parse_args()

    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        log("ERROR: chybí PREHRAJTO_EMAIL / PREHRAJTO_PASSWORD")
        return 2

    backlog = load_backlog()
    state = load_state()
    log(f"step=batch-start count={args.count} backlog={len(backlog)} "
        f"uploads={len(state.get('uploads', []))} "
        f"moderated={len(state.get('moderated_out', []))}")

    log("step=login")
    session = login(email, password)
    log("step=login done")

    succeeded = 0
    failed = 0
    extra_exclude: set[int] = set()
    t_batch = time.monotonic()
    for i in range(args.count):
        log(f"step=iteration {i+1}/{args.count}")
        film = pick_next(state, backlog, extra_exclude)
        if film is None:
            log("step=batch-end backlog vyčerpán")
            break
        extra_exclude.add(film["sledujteto_file_id"])
        if process_one(film, session, state):
            succeeded += 1
        else:
            failed += 1

    batch_sec = round(time.monotonic() - t_batch, 1)
    avg = round(batch_sec / max(succeeded, 1), 1) if succeeded else 0
    remaining = len(backlog) - len(state.get("uploads", [])) - len(state.get("moderated_out", []))
    eta_hours = round(remaining * avg / 3600, 1) if avg else None
    log(f"step=batch-end succeeded={succeeded} failed={failed} "
        f"total_uploads={len(state.get('uploads', []))} "
        f"batch_dur={batch_sec}s avg_per_film={avg}s "
        f"backlog_remaining={remaining} eta_hours={eta_hours}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
