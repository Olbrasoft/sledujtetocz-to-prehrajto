# Pokyny pro Claude Code

Tento repozitář je sesterský k [`prehrajto-sync`](https://github.com/Olbrasoft/prehrajto-sync),
jen zdroj filmů je **sledujteto.cz** místo **sktorrent.eu**. Cíl (Přehraj.to účet)
a celý upload flow jsou identické.

## Kontext projektu

Olbrasoft provozuje `ceskarepublika.wiki`. Pro případ výpadku primárních zdrojů
mirrorujeme filmy na vlastní Přehraj.to účet. Tento repozitář řeší sledujteto.cz
jako další záložní zdroj (paralelně k sktorrentu).

**Uživatel komunikuje česky**, kód a commits anglicky.

## Architektura

Viz [README.md](README.md). Stručně:

1. `backlog/sledujteto-films.jsonl` — 636 filmů, export 2026-04-22.
2. `src/pick_next_film.py` — filtr `LANG_CLASSES` env (default CZ_DUB+CZ_SUB).
3. `src/resolve_sledujteto_cdn.py` — `POST <cdn>.sledujteto.cz/services/add-file-link`.
4. `src/prehrajto_upload.py` — **kopie 1:1 z prehrajto-sync** (login → prepareVideo → CDN upload).
5. `src/sync_batch.py` — orchestrátor.
6. `.github/workflows/sync.yml` — manual dispatch, timeout 350 min, commituje state zpět.

## Klíčová zjištění (2026-04-22)

### Hash per CDN host
Sledujteto podepisuje hash per-host: hash vydaný `www.sledujteto.cz` pro
data11 upload data11 odmítne jako `invalid-file`. **Proto voláme add-file-link
přímo na target CDN hostitele** (`https://data11.sledujteto.cz/services/add-file-link`).
Informace pochází z `cr/scripts/sledujteto-detect-audio.py:97`.

### `video_url` vs `download_url`
- `video_url` = `https://<cdn>.sledujteto.cz/player/index/sledujteto/<hash>` —
  funguje jako MP4 stream s HTTP Range (200/206). **Toto používáme pro download.**
- `download_url` = `https://<cdn>.sledujteto.cz/player/dl/<hash>` — 302 na
  `invalid-download` bez validní session (neřešíme).

### Geo-blokace
Proxy README (`Olbrasoft/SledujteToCzProxy`) varuje, že `data{N}.sledujteto.cz`
blokuje datacentrové ASN (Hetzner, Oracle). **GitHub Actions Azure-USA runner
může být v blacklistu** — proto `test-sledujteto-access.yml` nejdřív.

**Fallback options pokud geo-block:**
1. `sledujteto.aspfree.cz/Hash.ashx?id=...&key=...` — aspone proxy vrací fresh
   hash z CZ IP, ale má limit 10 GB/měs bandwidth → pouze pro hash-gen, ne pro
   samotný download.
2. Self-hosted runner v ČR (uživatel má domácí infrastrukturu).
3. Rozšířit aspone proxy o streaming endpoint (ale přetéct by bandwidth).

Praxe: v mé testovací cestě (domácí CZ IP, AS44489) data11 `video_url` vrátil
HTTP 206 s `video/mp4`, 1.98 GB file. Z GitHub runneru uvidíme v testu.

## Přístupy

- **Přehraj.to**: `filmy.prehrajto@email.cz` / `***REDACTED-PASSWORD***`
  (viz `~/Dokumenty/přístupy/prehrajto.md`, sdílený účet s prehrajto-sync).
- **Aspone proxy secret**: v `Olbrasoft/SledujteToCzProxy/src/SledujteToCzProxy/Hash.ashx`
  (const `SharedSecret`). Hodnota `***REDACTED-PROXY-SECRET***`.

## GitHub Secrets (nutné před prvním runem)

```
PREHRAJTO_EMAIL      = filmy.prehrajto@email.cz
PREHRAJTO_PASSWORD   = ***REDACTED-PASSWORD***
SLEDUJTETO_PROXY_KEY = ***REDACTED-PROXY-SECRET***
```

## Konvence

- Jazyk kódu a commitů: **anglicky**.
- Jazyk s uživatelem: **česky**.
- GitHub issues vždy přes skill `github-issues`.
- PRs musí mít Claude session marker na začátku body (viz user-level CLAUDE.md).

## Debugging tipy

| Symptom | Příčina | Fix |
|---------|---------|-----|
| `add-file-link` vrací `{"error":true}` | upload_id neexistuje / smazaný | přeskočit, zalogovat |
| `player/index` HTTP 403 z runneru | geo-blokace data{N} | přes aspone proxy / self-hosted |
| `player/dl` 302 invalid-download | chybí session cookie | použít `video_url`, ne `download_url` |
| Login na prehraj.to `redirect: /` místo `/?afterLogin=1` | chybí priming GET | `src/prehrajto_upload.py` to dělá automaticky |
| Upload 403 na api.premiumcdn.net | chybí `Referer: https://prehraj.to/` | stejně, code má to správně |

## Co zbývá

1. **Nasadit repo na GitHub** — `gh repo create Olbrasoft/sledujtetocz-to-prehrajto --private`.
2. **Nastavit Secrets** — trojici výše.
3. **Dispatch `test-sledujteto-access.yml`** — ověřit, že runner dosáhne data{N}.
4. **Dispatch `sync.yml` s batch_size=1** — první ostrý pokus.
5. Až bude OK, enable schedule cron (v workflow zatím zakomentované).
