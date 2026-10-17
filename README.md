# Topshot

Local-first photo culling for macOS. Import a shoot, review AI suggestions, choose how many
photos to keep, and copy the confirmed keeps to a folder without ever touching the originals.

This repository contains the first end-to-end vertical slice:

**profile → source folder → recursive ingest → proxies → heuristic analysis → review grid + loupe →
keep target / manual edits → confirmed destination → SHA-256 verified copy**

## Safety guarantees

- Source folders are opened **read-only**. Topshot never deletes, moves, renames, or writes into
  the source folder, and never writes ratings or metadata back to photos or sidecars.
- "Reject" only excludes a photo from the copy. The original stays where it is.
- Only checked/confirmed keeps are copied, and only after the confirmation dialog.
- Copies are streamed to a hidden temporary file, SHA-256 verified, then atomically linked into
  place. Existing files are never overwritten.
- Repeat runs detect byte-identical destination files and skip them instead of creating suffixes.
- RAW+JPEG pairs copy together by default. Matching XMP sidecars are copied alongside their image.
- All processing is local. There are no model downloads or network calls in this increment.

## Prerequisites

- macOS 14+ on Apple Silicon
- Node.js 20+ (tested with Node 26)
- Python 3.11 (Homebrew: `brew install python@3.11`)
- Rust stable (`rustup`; needed for Tauri). If missing:
  `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`

## Setup

```bash
cd Topshot

/opt/homebrew/bin/python3.11 -m venv engine/.venv
engine/.venv/bin/pip install -r engine/requirements-dev.txt

npm install
```

## Run the desktop app

```bash
npm run dev
```

Tauri starts the Python engine as a localhost sidecar. The engine prints its port and a random
access token to the parent process; all RPC and proxy requests are authenticated with that token.
The app store lives at:

```
~/Library/Application Support/Topshot/
  topshot.sqlite3
  proxies/<project-id>/
```

## Workflow

1. **New Shoot** (or drag a folder onto the window). Scanning is recursive. JPEG, HEIC, and
   CR2/CR3/NEF/ARW/RAF/DNG/ORF/RW2 are ingested. MOV/MP4 (and other video) and unsupported files
   are counted and skipped without aborting the run.
2. Proxies (long edge 1600) and thumbnails (long edge 320) are generated in the app cache.
3. Heuristic analysis scores:
   - focus (variance of Laplacian),
   - visual interest (colorfulness, contrast, edge density),
   - people (OpenCV Haar face detection plus eye and smile cascades),
   - duplicate/similar groups (dHash + DCT pHash, cross-burst) and burst grouping
     (capture time + body + visual similarity).
4. Review:
   - **Keep top N** applies a cull target. AI selects exactly N photos by composite rank
     (sharpness, visual interest, people), taking one pick per burst first. The rest become
     alternates/rejects. Manual selections are preserved, count toward the target, and the AI
     fills the remaining slots; re-culling never overrides a manual pick.
   - Filter chips: All / Keeps / Review / Rejects / Selected / People / Similar / Docs.
   - Bursts and similar/duplicate groups are collapsed to a cover frame with a count badge;
     **B** or the expand button opens the strip. The best frame in a similar group is marked
     **Best**, and the group header explains that eyes, smiles, and visual quality were used.
   - Inside an expanded similar/burst group, every member has **Use this instead** / **Current
     pick**. Swapping selects the chosen frame, marks it **Your pick**, deselects the previous
     pick, and keeps the group's copy count unchanged. **⌘Z** restores the previous pick.
   - Opening any burst or similar frame in the loupe shows a frame strip along the bottom with
     every alternative, the AI **Best** frame, the current pick, and **Use instead** buttons, so
     you can compare and swap without returning to the grid. Loupe undo works too.
   - Double-click or **Space** opens the loupe with fit / 100% zoom (100% is the 1600px review proxy).
   - Keyboard: `1–5` rate, `S` select/deselect, `X` exclude, `U` unrate, arrows navigate,
     `B` toggle burst, `⌘Z` undo (session only), `Esc` close loupe. Rating, select, exclude,
     unrate, preview, and burst shortcuts act on the **hovered** tile when the pointer is over
     the grid, otherwise on the focused tile. A compact shortcut summary sits in the top banner.
   - Loupe keys: the same rating/select/exclude/unrate shortcuts plus `←/→` group navigation
     scoped to the active grid filter, `↑/↓` alternatives within the current group (group
     navigation uses a snapshot of the filter set, so it stays stable after unselecting a photo),
     click/`Space`/`F` four-stage zoom (fit → 1.5× fit → 100% → 200% proxy), `B` toggle the filmstrip,
     `Enter` use-instead for an alternate, `⌘Z` undo, `Esc` close. Hover a filmstrip frame to
     preview it; the cursor is a magnifying glass with `+`/`−` for the next zoom. The loupe
     header shows its own shortcut legend, and the footer shows remaining similar/duplicate
     groups (`x / y left`) for the filter set the loupe was opened from.
   - **Docs** flags note-taking frames (documents, signs, menus, maps, screens, appliances) with a
     **Doc** badge and excludes them from AI keeps.
   - AI keeps are preselected. Every selection can be overridden.
5. **Copy Keeps**:
   - destination folder picker (remembered per project),
   - confirmation with file count, total size, source, destination, pair setting, and missing files,
   - resumable/cancelable copy with per-file progress,
   - completion summary with copied/skipped/failed/missing counts and **Open Destination**.
6. **Rescan** appends new photos, retains decisions, detects changed files, and marks removed
   sources as missing.

## Profiles and personalization

- The app supports **local profiles** for several people sharing one Mac. The active profile is
  shown in the sidebar and top banner; switch, create, rename, or delete it from the sidebar.
- Photo content is shared: one analysis pass per project supplies files, proxies, scores, scene
  tags, bursts, and similar groups to every profile.
- Review state is isolated per profile: selections, ratings, rejects, keep target, preset,
  destination, RAW+JPEG mode, copy runs, and learned preferences.
- Existing installations migrate to a seeded **Default** profile. A profile switcher is available
  before any project is opened.
- **Personalization** learns only from explicit edits for the active profile—manual select/deselect,
  reject, rating, and group swaps. It combines a scene-label keep-rate profile with a small local
  logistic ranker over the existing image features. The adjustment is capped at ±0.08, begins only
  after 20 explicit examples, never overrides documentation exclusion or duplicate diversity, and
  can be reset from the sidebar. No data or models leave the machine.
- Profiles are organizational separation, not encryption: the SQLite database is readable by any
  process running as the same macOS user. Use separate macOS accounts for a hard privacy boundary.

## Analysis and ranking (local heuristics, no model downloads)

- **Focus:** variance of Laplacian on the 320px thumbnail. Conservative preset only rejects clear
  focus misses; borderline frames are surfaced for review. Presets: conservative / balanced /
  aggressive.
- **Visual interest:** percentile combination of colorfulness (Hasler–Süsstrunk), luma contrast,
  edge density, and sharpness.
- **Documentation detection:** the macOS Vision framework (built into the OS; no download or
  upload) tags scene labels and runs OCR on each preview. Strong document/sign/menu/map labels,
  many OCR text lines, or a dominant appliance/electronics label mark a frame as documentation.
  Those frames get `review`, a **Doc** badge, and are skipped by the keep target. Frames whose
  subject is clearly people are protected. On non-macOS hosts or when `swiftc` is unavailable,
  tagging is skipped and ranking falls back to the visual heuristics.
- **People:** faces detected with bundled OpenCV Haar cascades; overlapping detections are merged.
  Eye cascades (including an eyeglasses cascade) and a smile cascade run inside each face.
  This is a heuristic, not a face-recognition or pose model; it can undercount crowds, profiles,
  glasses, and angled faces.
- **Rank:** photos with people use
  `0.38 × sharpness + 0.18 × interest + 0.16 × people presence + 0.23 × face quality + 0.05 × exposure`,
  where face quality combines open-eye and smile detections. Photos without people use
  `0.55 × sharpness + 0.40 × interest + 0.05 × exposure`, so landscapes are judged on how good
  they look.
- **Bursts:** same camera body, ≤ 2 s apart (rapid frames ≤ 1.2 s regardless of content; longer
  gaps also require perceptual-hash similarity). Best pick uses the composite rank.
- **Similar/duplicates:** dHash ≤ 8 or DCT pHash ≤ 8 within 2 hours, cross-burst, grouped in the
  UI. The best frame uses the same composite rank (eyes, smiles, visual quality).
- **Target selection:** one frame per burst or similar group, then a diversity pass rejects any
  candidate that is still near-identical (dHash ≤ 10 or DCT pHash ≤ 10) to an already selected
  frame. This is why a target of 50 cannot fill up with duplicates.

## Tests and verification

```bash
# Engine unit tests (ingest, proxies, bursts, duplicates, target selection, copy edge cases)
npm run test:engine
# or: cd engine && .venv/bin/python -m pytest -q tests

# End-to-end engine test against a local photo folder
# Set TOPSHOT_E2E_SOURCE to the folder you want to test with.
engine/.venv/bin/python scripts/e2e_engine.py            # first run: import + copy
engine/.venv/bin/python scripts/e2e_engine.py --no-import # copy-only re-verification

# Headless UI verification (requires the engine + Vite running, see below)
npm run test:ui
```

For the browser UI test, run the engine with a known token and start Vite with that port/token:

```bash
TOPSHOT_TOKEN=ui-test-token engine/.venv/bin/python -m topshot_engine.server --port 8731
npm run test:ui              # opens the Vite preview with ?enginePort=8731&engineToken=...
```

The UI test uses the system Google Chrome via `puppeteer-core`. It creates screenshots in
`test-output/screens/`.

### Verification results from the vertical-slice run

- Import: **3,740** supported photos (2,781 JPEG + 959 HEIC) from the test shoot; **132** videos
  skipped; **1** PNG unsupported; **0** per-file failures.
- Proxies: 1.3 GB cached under `~/Library/Application Support/Topshot/proxies/1`.
- Analysis: 459 bursts, 137 similar/duplicate groups (827 photos), 1,480 photos with detected people.
- Keep target 500 after the duplicate/diversity pass: exactly **500** selected, **0** near-identical
  pairs at dHash/pHash ≤ 10, 183 photos with detected people, **0** documentation frames selected.
- Scene tagging: **89** documentation candidates flagged (documents, signs, menus, maps, screens,
  appliances); the laundry-machine frame and text signs are no longer in the AI keeps.
- Engine tests: **14 passed**, including two-profile decision/settings/copy isolation and
  per-profile preference event/model isolation. Headless UI verified the profile switcher,
  personalization panel, loupe fit (no cropping), loupe shortcuts (`S`, rating/unrate, arrows,
  Space zoom, `B` strip toggle, `Esc`), hover-targeted `S`, similar-group expansion, swap/undo,
  target selection, Docs filter/badge, and the verified copy summary (shown with the active
  profile).
- Real copy (mixed JPEG/HEIC, 40.4 MB total): every source/destination SHA-256 matched; a
  pre-existing destination collision was renamed to `name (1).ext` and never overwritten; a repeat
  run skipped all 5 identical files without creating new names.
- XMP sidecar copy and RAW+JPEG pair modes covered by unit tests; UI flow copied 4 images + 1 XMP.
- **All 3,873 source files were byte-identical before and after** (SHA-256 manifest diff).

## Project layout

```
engine/topshot_engine/
  config.py       paths, supported extensions
  db.py           SQLite schema, WAL, migrations
  ingest.py       recursive scan, sidecar/RAW pairing, upsert/rescan
  imaging.py      metadata, preview/thumb, sharpness, interest metrics, Haar faces
  analysis.py     percentiles, bursts, duplicates, suggestions, personalized keep-target ranking
  preferences.py  per-profile preference events, local model training, capped rank adjustment
  jobs.py         resumable pipeline jobs, progress, cancel
  copy.py         plan/expand pairs, preflight, verified copy, collision/skip logic
  rpc.py          JSON-RPC methods, profile/settings/preference APIs
  native/         macOS Vision Swift probe (scene labels + OCR)
  server.py       localhost HTTP server + authenticated proxy endpoints
src/              React review UI (grid, bursts, loupe, target, copy dialogs)
src-tauri/        Tauri shell, engine sidecar lifecycle, folder dialog, Open Destination
scripts/          e2e engine test and headless UI check
```

## Known gaps in this increment

- Eye/smile detection is Haar-cascade based and can miss glasses, profiles, small faces, and
  crowds; it is a ranking signal, not an eyes-closed verdict. Duplicate grouping can chain a long
  scene into one large similar group.
- Documentation detection uses the macOS Vision framework. The small Swift probe is compiled on
  first use; without macOS/`swiftc` the tagging stage is skipped.
- 100% loupe zoom is 1:1 on the 1600px proxy, not the original RAW/HEIC pixels.
- Preference learning is a lightweight local label + logistic model; it does not yet use Vision
  feature prints or embeddings, and the per-project vs global model split is simplified to one
  model per profile.
- Profiles are not a security boundary (no PIN or encrypted store). Use separate macOS accounts
  for privacy.
- No CSV/JSON report, contact sheet, project deletion UI, or model-download consent flow.
- Analysis/copy resume after a crash uses persisted rows and idempotent skips; in-memory job status
  itself is not persisted across engine restarts (the project shows "interrupted" and can resume).
- `npm run dev` is the supported run path. A packaged `.app` is not yet self-contained with the
  Python venv; `TOPSHOT_ROOT` can point a built app at this repository.
- Undo is session-only (including group swaps).
- Manual selections survive re-culling and count toward the keep target; the AI fills the rest.
- Existing projects need a Rescan (or recull) to compute new documentation metrics after
  upgrading; the profile migration itself preserves the existing selected set.
- New profiles on an existing project start with no keep target and therefore select all baseline
  AI keeps until that profile sets its own target.
