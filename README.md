# Flashbang

Structure-aware corruption engine for Flash (`.swf`) games.

Random byte-flipping on a SWF almost never works — Flash Player validates the
container before it runs anything, so you get a white screen instead of a
glitch. Flashbang parses the tag stream first and only damages byte ranges
that are known to survive.

## Safety rules

Every corruption is **length-preserving**, so no offset in the file ever shifts.

| Never touched | Why |
|---|---|
| SWF header (signature, version, fileLength, stage rect, framerate) | Player rejects the file outright |
| Tag codes and tag lengths | One bad length desynchronises the whole tag stream |
| Character IDs (first 2 bytes of every `Define*` tag) | Breaks every reference to that asset |
| `SymbolClass`, `ExportAssets`, `FileAttributes`, `DefineSceneAndFrameLabelData` | AS3 class binding — instant crash |
| ActionScript opcodes, jump offsets, constant-pool indices | VM aborts on the first bad instruction |
| Symbol-looking strings (`gotoAndStop`, `flash.display.Sprite`, `_root`) | Name lookup fails, movie dies |
| Exponent bytes of float constants | A stray `Infinity` turns into a hung loop |
| JPEG `0xFF` markers / MP3 frame syncs | Decoder gives up entirely instead of glitching |

## Targets

- **`graphics`** — shape records, morph shapes, fonts, buttons, video
  frames, JPEG entropy data, lossless bitmap pixels (decompress → corrupt →
  recompress into the same slot), plus `PlaceObject2/3` matrices and colour
  transforms at the bit level.
- **`sound`** — `DefineSound` payloads and `SoundStreamBlock` music, with
  format-aware header skipping and sync-safe byte selection.
- **`logic`** — AS2 `ActionPush` / `ActionConstantPool` literals, and the AS3
  ABC constant pool (doubles + display strings). Numbers drift, on-screen text
  garbles, code keeps running.
- **`text`** — glyph indices in `DefineText` / `DefineText2` (remapped within
  each font's glyph count, so "SCORE" jumbles while the tag stays valid) and
  the initial-text string of `DefineEditText` (printable-only scramble).
  Symbol names and variable names are left alone.

`-t all` does all four, each with its own strength.

### Sub-modes

These change *how* a target is damaged. Each is opt-in.

- **`--asset-swap`** (graphics) — exchanges `CharacterId`s between placements of
  the same category (shape↔shape, bitmap↔bitmap). The player becomes a tree;
  structurally safe because every id stays a valid, same-type id.
- **`--opswap`** (logic, AS2) — swaps operators for same-length siblings:
  `+`↔`-`↔`*`↔`/`, `<`↔`>`, `&`↔`|`↔`^`, `>>`↔`>>>`. Gravity goes up, score
  goes down, win becomes lose. (AVM2/AS3 falls back to constant-pool
  corruption — safe operator swapping there needs a full bytecode walker.)

## Windows build

**On your own machine.** Double-click `build_windows.bat`. It needs Python 3.9+
from python.org with *Add to PATH* ticked, installs PyInstaller itself, and
drops two self-contained binaries in `dist\`:

| | |
|---|---|
| `Flashbang.exe` | the GUI, windowed, no console box |
| `flashbang-cli.exe` | the command line version |

Neither needs Python on the target machine. Drop `ruffle.exe` next to
`Flashbang.exe` and the *Open in Ruffle* toggle finds it with no configuration.

**On CI.** `.github/workflows/build-windows.yml` builds both on every push and
uploads them as an artifact; push a `v*` tag and they get attached to the
release.

Either route uses the same `flashbang.spec`, which sets the icon, the Windows
version resource, and excludes the scientific stack so the binaries stay small.
One-file PyInstaller builds do sometimes trip heuristic antivirus — switch
`EXE(...)` to a `COLLECT` folder build if that becomes annoying.

## GUI

```
python3 flashbang_gui.py            # or drop a file on it: flashbang_gui.py game.swf
```

`flashbang_gui.py` must sit next to `flashbang.py`. Tkinter only — on Arch that
means `pacman -S tk`.

It opens in **simple mode**: one *Corruption* slider driving all four targets
together. The `Advanced ▸` link in the corner splits it into a per-target
slider with on/off switches; `◂ Simple` folds it back. Values carry across both
ways — simple → advanced copies the dial onto every target, advanced → simple
averages the enabled ones.

- **Analyse** runs a dry scan and prints the tag inventory plus how many bytes
  each target can actually reach.
- Once analysed, the slider shows a live estimate (`~1,240 of 812,004 B`) so
  you can dial in the damage before writing anything.
- **Modes** row — the two sub-modes (asset swap, operator swap) as checkboxes.
  Toggling one re-analyses, since it changes the corruptible surface.
- **Randomize** (`Ctrl+R`) rolls the dials and a fresh seed. In simple mode
  that is one number; in advanced mode it switches on a random subset of
  targets — always at least one — and rolls each separately, skipping any
  target the loaded file cannot reach.
- **Feed back ↻** uses the last output as the next input, bumping a
  *generation* counter. One click makes it worse; repeat for a progressive
  decay arc. Picking a new input by hand resets the lineage.
- **History** panel — every run with its time, strengths, seed, modes and
  generation. Double-click or *Restore selected* to load those settings back.
- Every write is **structure-checked** before it ships; a file that would not
  load is refused rather than saved.
- **Open in Ruffle** launches the finished file in the Ruffle desktop player
  the moment it is written. Any Ruffle already running is closed first, so you
  never end up with a pile of windows — including instances Flashbang did not
  start. Ruffle is found on `PATH`, in `/Applications` on macOS, or as the
  `rs.ruffle.Ruffle` flatpak; override with the `FLASHBANG_RUFFLE` environment
  variable, or point at it by hand when prompted.
- Seed is always visible and editable — copy it down if a run looks good.
- `Ctrl+R` randomize, `Ctrl+Enter` run.

## CLI

```
flashbang.py game.swf --report
flashbang.py game.swf -o out.swf -t all -s 30
flashbang.py game.swf -o out.swf -t graphics,text -s graphics=60,text=40
flashbang.py game.swf -o out.swf -t graphics --asset-swap
flashbang.py game.swf -o out.swf -t logic --opswap -s 100
```

| Flag | Meaning |
|---|---|
| `-t, --target` | `graphics`, `sound`, `logic`, `text`, `all` (comma separated) |
| `-s, --strength` | `0`–`100`, or per target: `graphics=60,text=40` |
| `--asset-swap` | graphics: swap same-type character references |
| `--opswap` | logic: swap operators instead of bytes (AS2) |
| `--seed N` | Reproducible runs — the same seed gives the same file |
| `--wild` | Unlocks riskier regions: shape bounds, matrix sign bits, background colour, `DefineBinaryData` |
| `--report` | Analyse only — tag inventory and corruptible surface, writes nothing |
| `--compress` | `keep` (default) / `yes` / `no` |

Strength is a geometric ramp: **~10** is barely perceptible, **~50** is clearly
glitched but playable, **~90+** shreds the asset.

## Notes

- Handles `FWS`, `CWS` (zlib) and `ZWS` (LZMA) input. LZMA input is re-emitted
  as `CWS`, which every player from v6 up reads.
- `DefineSprite` is walked recursively, so nested timelines are covered.
- Unparseable tags are skipped and left byte-identical rather than guessed at.
- Stdlib only — no dependencies.
- Test it in Ruffle first; it is stricter than Flash Player was, so anything
  that survives Ruffle will survive a projector.

`--wild` is the one that breaks things. That is the point of it.
