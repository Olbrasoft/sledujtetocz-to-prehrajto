# sledujtetocz-to-prehrajto

Automatická synchronizace filmů **sledujteto.cz → Přehraj.to**.

Protějšek k [`prehrajto-sync`](https://github.com/Olbrasoft/prehrajto-sync) (který
mirroruje sktorrent.eu). Stejný cílový účet, stejný upload flow — jen jiný
source CDN.

## Architektura

```
┌─ GitHub Actions cron (ubuntu-latest, 14 GB disk) ─────────┐
│                                                           │
│  1. git clone sledujtetocz-to-prehrajto                   │
│  2. python pick_next_film.py  →  další kandidát           │
│  3. POST data{N}.sledujteto.cz/services/add-file-link     │
│     → video_url (hash-signed, per-CDN-host)               │
│  4. curl → /tmp/film.mp4 (Range supported, ~1–5 GB)       │
│  5. python prehrajto_upload.py /tmp/film.mp4              │
│  6. git commit state/uploaded.json + push                 │
└───────────────────────────────────────────────────────────┘
```

## Zdroj dat

- `backlog/sledujteto-films.jsonl` — 636 filmů, vyexportováno 2026-04-22
  z `ceskarepublika.wiki` DB. Obsahuje `sledujteto_file_id`,
  `sledujteto_cdn` (`data10` / `data11`), `title`, `year`, `description`,
  `cr_film_id`, `cr_slug`, `lang_class` (CZ_DUB / CZ_SUB / EN), …

## Klíčové zjištění (2026-04-22)

- **Hash-per-host**: `add-file-link` musí jít přímo na CDN host filmu
  (`data11.sledujteto.cz`, ne `www`), jinak data{N} odmítne hash jako
  `invalid-file`. Viz `cr/scripts/sledujteto-detect-audio.py:97`.
- `www.sledujteto.cz` CDN je ASN-agnostické; `data{N}` blokuje datacentra
  (Hetzner, Oracle — GitHub Actions Azure USA **pravděpodobně blokované**,
  ověří `test-sledujteto-access.yml`).
- `GET /player/dl/{hash}` chce session — lepší používat `video_url` z
  `add-file-link` přímo (vrací `/player/index/sledujteto/{hash}`, podporuje
  HTTP Range) a stahovat to jako obyčejný MP4.

## Credentials

- Přehraj.to účet: `filmy.prehrajto@email.cz` (stejný jako prehrajto-sync,
  viz `~/Dokumenty/přístupy/prehrajto.md`).
- Sledujteto proxy secret: `~/Olbrasoft/SledujteToCzProxy/src/.../Hash.ashx`.

## Quickstart

1. **Přečti [CLAUDE.md](CLAUDE.md)** — handoff pro novou session.
2. **`.env`** z `.env.example`, nebo GitHub Secrets.
3. Test konektivity: dispatch `.github/workflows/test-sledujteto-access.yml`.
4. První běh: dispatch `.github/workflows/sync.yml` s `batch_size=1`.

## Licence

Interní projekt Olbrasoft. Žádná veřejná licence.
