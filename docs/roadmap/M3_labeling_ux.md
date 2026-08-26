# M3 — Label screen redesign (UX)

> Supplements `docs/roadmap/M3_labeling.md`, which specifies the data model, the
> queue and the routes. This document specifies **the screen**: what it looks
> like, what happens on each keypress, what the nine class glyphs are, and how we
> will know it worked. Nothing here changes the instance key, the write
> semantics, or the agreement SQL.
>
> The interaction model is **not** open. Six decisions are fixed by the user and
> everything below is designed inside them:
> ArrowLeft/ArrowRight navigate and ArrowLeft *is* the undo; questions are
> answered sequentially with the active one binding 1..9; every class has an
> icon; auto-save is always on and there are no preferences at all; there is no
> all-keys overlay and no global definitions toggle; skipping is abolished and
> FLAG requires a reason and a written explanation.

---

## 1. What is wrong with the current screen

The complaint is "not very intuitive". The cause is geometry, and it is
measurable from the source rather than from taste.

`cownting/labeling.py:578` forces a square crop; `Label.tsx:830` renders it at
`max-w-[440px]`. So the image is **440 px tall**. `LabelGroup.tsx` builds
option rows of 42 px (`py-2` + `Kbd h-6` + 2 px border) with `gap-2`, giving
224 px for Sun exposure, 274 px for Behaviour and 20 px between: **518 px of
answer area**. Measured from the ringed animal — the only fixation point that
matters:

| target | distance below the ring |
|---|---|
| Sun option 1 | 293 px |
| Sun option 4 | 443 px |
| Behaviour option 1 | 537 px |
| Behaviour option 5 | 737 px |
| Save button | 814 px |

The fovea resolves roughly 2° of visual angle — about **80 CSS px at a 60 cm
viewing distance**. Every answer target is 3.7×–9.2× that radius away from the
cow being judged. Nothing on this screen can be seen at the same time as the
animal.

Worse, in absolute page coordinates (sticky header 73 px from
`index.css:68`, `main py-10`, page header block ~153 px, `gap-8`, `Card p-6`)
the card content starts at y≈322, the crop bottom is y≈790, Behaviour occupies
y≈1054–1328 and Save sits at y≈1384. A 1920×1080 laptop offers ~950 px of
viewport. **At scroll-top the annotator cannot see the second question or the
Save button they are required to press.** On a 1366×768 machine (~620 px) the
crop itself is cut in half.

Six further defects, each of which the redesign has to fix explicitly:

1. **Three to four saccades plus a scroll per item.** A gaze round trip costs
   ~350–500 ms (saccade latency 175–200 ms, flight 30–120 ms, fixation
   200–250 ms). The perceptual work — lit or shaded, up or down — is finished
   inside 400–600 ms (Thorpe, Fize & Marlot 1996, *Nature* 381:520–522: ~92%
   correct at 390 ms RT on 20 ms presentations). Everything after that is our UI.
2. **Nothing marks the digit rebind.** `labelKeys.ts:112` gives every group the
   same 1..9 and the module comment says "the digits mean something different a
   moment later — that is the point", but `LabelGroup.tsx:159-211` renders both
   groups identically and `disabled` is one page-wide boolean. Key `2` meaning
   *Direct sun* then *Lying* is a **varied mapping across contexts**; Schneider &
   Shiffrin (1977) show varied mapping never becomes automatic. The display has
   to carry the cue the annotator's memory cannot.
3. **The icons defeat their own purpose.** `ClassIcon.tsx` renders at a default
   `w-4 h-4` = 16 px with `strokeWidth="2"`; `shade` (arc + bars + 1.3–1.8 unit
   ticks) and `sun` (disc + 8 rays) share a silhouette at that size. This is the
   exact failure of Lucide `sun` vs `sun-dim`, whose raw SVGs are both
   `<circle cx=12 cy=12 r=4/>` plus eight marks at the same eight radial
   positions. Sun exposure is the variable the study turns on.
4. **Full-width rows with the info icon floated ~800 px right.**
   (`LabelGroup.tsx:105-127`.) Rows are 42 px — already at WCAG 2.2 SC 2.5.5's
   44×44 AAA scale in the useless dimension — while wasting all the width. No
   tool in this space does this; CVAT renders `1: Shaded` as one tight token
   (`attribute-editor.tsx`), Prodigy columnises image options.
5. **Auto-save blocks the keyboard.** `doSubmit` sets `busy` and awaits the POST
   (`Label.tsx:456-485`); `applyAnswer` early-returns on `busy` with no feedback
   (`Label.tsx:569`). A keypress during the flush is dropped in total silence —
   and the annotator's reflex is to press again, which lands on the *next*
   instance. Prodigy ships `instant_submit: false` with `batch_size: 10` for
   precisely this reason.
6. **Going back re-commits.** `Label.tsx:589` fires the submit whenever
   `firstUnansweredIn(next) === null`, which on an item revisited via ArrowLeft
   is true the moment you touch *any* option. Correcting Q1 advances past Q2.
   With Skip abolished, ArrowLeft is the only recovery mechanism, and it is a
   trap.

**Honest arithmetic**: ~2.0–2.5 s of pure UI overhead per item on top of a
~2.5 s perceptual-plus-decision core. An achievable target is 2.5–4 s per
instance. The delta is ~2 s × 2,800 instances ≈ **93 minutes per annotator per
day of footage**, times many days. That gap is the entire business case.

**What is already right and must survive the rewrite.** `InstanceCrop.tsx` —
server-built crop, client-drawn SVG ring, dual dark-under-light stroke so the
ring survives both a sunlit flank and panel shade, `aspectRatio` reserved so
nothing jolts, `key={crop_url}` so a stale cow is never answerable.
`activeElapsedMs` (`Label.tsx:212`) is a visibility-aware active clock, not wall
time. `labelKeys.ts:86-93` tie-breaks `sort_order` on the immutable `class_key`
and mirrors the server's own `ORDER BY`, so a rename cannot move a key — that is
the frozen-order discipline positional digits depend on, and it is doing real
work. The seeded definitions in `labels_db.py:637+` are written in the
operational-postural style that got video cattle-behaviour studies to Cohen's
κ = 0.84. The burnt-in timestamp is masked server-side so time-of-day cannot hand
over the sun answer. `question` reused as the icon for both escape classes is the
correct call.

---

## 2. The redesigned layout

### 2.1 The rule the geometry serves

**One item = one screen = one fixation region + one saccade per question.**
The whole per-item loop — crop, active question, step counter, flag affordance,
sync state — must fit in the viewport of a 1366×768 laptop at `scrollY = 0`.
Nothing about answering an instance may require a scroll, ever.

### 2.2 Page chrome (Label route only)

The Label route gets its own dark surface, scoped by a `data-surface="label"`
attribute on the route wrapper so the rest of the app keeps its theme. This is a
**correctness argument, not a taste one**: Q1 asks the annotator to judge
brightness, and an 888 px near-white card around a 440 px photograph biases
perceived brightness of the very region being judged. Diagnostic reading rooms
are specified at roughly 25–75 lux (ACR/AAPM TG-18 lineage) for the same reason.
The crop must be the brightest thing on screen.

| token | value | use |
|---|---|---|
| `--lbl-bg` | `#16181A` | page background |
| `--lbl-card` | `#1E2124` | main column and side panel |
| `--lbl-tile` | `#262A2E` | unselected tile |
| `--lbl-ink` | `#E8EAEC` | primary text |
| `--lbl-ink-dim` | `#9AA1A7` | labels, hints |
| `--lbl-q1` | `#E0A03C` | Sun exposure accent (warm) |
| `--lbl-q1-field` | `#2A2418` | Sun exposure panel field |
| `--lbl-q2` | `#3FB8AE` | Behaviour accent (cool) |
| `--lbl-q2-field` | `#152526` | Behaviour panel field |
| `--lbl-alarm` | `#C8CDD2` + hatch | flag / defect states (see §5) |

Two hues plus neutral. No third hue anywhere in the answer area.

The page header block on this route collapses to a single 28 px strip
(dataset name, camera, frame time, right-aligned and dim). `main` padding drops
from `py-10` to `py-4`.

### 2.3 Vertical budget, stated as arithmetic

Below the 73 px sticky header, a 1366×768 browser gives ≈620 px of viewport.

```
  page strip                 28
  main py-4 (top)            16
  card padding (top)         16
  crop                        C
  gap crop→panel               8
  question panel:
    prompt row               24
    gap                       8
    tile row                108
    panel padding (2×10)     20
  gap panel→footer            10
  footer strip               32
  card padding (bottom)      16
  main py-4 (bottom)         16
  ---------------------------------
  fixed chrome              302 + C
```

So `C = clamp(300px, calc(100vh - 414px), 440px)` — 414 accounts for the 302
above plus ~112 px of browser chrome. On 1366×768 the crop renders at **354 px**;
on 1920×1080 at the full **440 px**; it never drops below 300 px. The crop is
square (`labeling.py:578`) so width tracks height.

**Resulting eye travel.** The ring is at the crop's centre. On a 1366×768
machine the crop centre is 177 px from the crop top; the tile row centre is at
354 + 8 + 24 + 8 + 10 + 54 = 458 px, i.e. **281 px below the ring** — and the
*furthest* target is the same 281 px, because the options are a single row rather
than a stack. Compare the current screen: 293 px to the nearest target, **737 px
to the furthest**, plus a scroll. The redesign turns 3–4 saccades and a scroll
into **one saccade**, because the whole option row (≤ 600 px wide, 108 px tall)
falls inside a single fixation once the eye lands on it.

We are not pretending this reaches the ~80 px foveal radius. It cannot: the
target must be outside a 354 px image whose centre is the fixation point. The
achievable win is *one* saccade per question instead of three plus a scroll, and
that is what the geometry above buys.

### 2.4 Main column

Max width **760 px**, centred; side panel **320 px**, `position: sticky` at
`calc(var(--app-header-h) + 16px)`. Gap 24 px. Total 1104 px, comfortable at
1280 and up; below 1100 px the side panel drops beneath the fold (it carries
nothing needed to answer an item) and the main column keeps its geometry
unchanged.

**Crop.** `InstanceCrop` unchanged in behaviour, re-sized by the clamp above.
`SCRIM_OPACITY` stays 0.45. Holding **Space** lifts the scrim to 0 and scales the
crop to the full available column height for as long as the key is down; release
restores; advancing restores. Hold, never toggle — a mode you can be stranded in
is worse than no mode. This exists because Sun exposure is judged from the
*surroundings*, and Encord's Turbo mode documents exactly this manual-pull-back-
then-auto-re-zoom behaviour.

**Question panel.** One rectangle, 8 px below the crop, `border-radius: 10px`,
`border-left: 3px solid` in the active question's accent, background the active
question's field colour. Contents:

- **Prompt row, 24 px.** Left: the question's glyph at 16 px, then the question
  name in 13 px `600` uppercase-tracking. Right: the step counter rendered
  literally as `[1/2]` / `[2/2]` — this is CVAT's
  `"[${currentIndex + 1}/${attributesCount}]"` from `attribute-switcher.tsx`,
  stolen verbatim, and it is the only thing that tells a sequential-flow
  annotator that a second question exists — followed by a two-segment pip `●○`.
- **Tile row, 108 px.** `display: grid`, `grid-auto-flow: column`,
  `gap: 10px`, tiles `112 × 108`. Four tiles for Sun exposure = 478 px; five for
  Behaviour = 600 px; six (once `behaviour.not_visible` lands, §6.3) = 722 px,
  still one row inside 760. The row is **left-aligned, never centred** — centring
  would move tile 1 horizontally between a 4-up and a 5-up question, and tile 1's
  position is what muscle memory aims at.

**Tile anatomy** (112 × 108, `--lbl-tile`, 1 px border, radius 8):

```
┌────────────────┐
│ ⓵           ⓘ │   badge 20×20 top-left (digit, 12px 700, accent on dark)
│                │   info dot 24×24 hit box top-right (see §3.6)
│       ◣        │   glyph 26×26, centred, currentColor
│                │
│    Shaded      │   label 12px/14, centred, up to 2 lines, never truncated
└────────────────┘
```

112 × 108 is **2.5× WCAG 2.2 SC 2.5.5's 44×44 AAA target**, and the 10 px gutter
satisfies SC 2.5.8's spacing clause. The badge, glyph and word are one visual
token with no interior whitespace — CVAT's `1: Shaded` adjacency, which is the
direct fix for the "lot of horizontal whitespace" complaint.

The word stays. NN/g is explicit that icon labels must be visible at all times
without interaction; the icon is the *acquisition* aid and the label is the
*disambiguation*. "Head probing" is a coined domain term no glyph will ever carry
alone. The compression comes from the tile layout, not from deleting text.

**Footer strip, 32 px.** Left: `F flag · ← back · → next · hold Space inspect`
in 11 px dim text — permanently printed, which is CVAT's shipping alternative to
a `?` overlay and satisfies decision 5. Right: the sync dot (§3.5) and the
instance provenance (`cam03 · 2026-05-14 09:41`).

**FLAG lives in the footer, never as a tile.** BI-RADS keeps "incomplete"
(category 0 — critical information is missing) structurally apart from the
diagnostic categories 1–6 for exactly this reason. `Cannot tell` is a final
answer meaning the pixels do not decide it; FLAG means the *item* is defective.
Render them as siblings and FLAG becomes the Skip we just abolished.

### 2.5 Side panel (320 px) — never needed to answer an item

Four blocks, top to bottom:

1. **My stream.** `412 / 1 400 today` with a bar, then `11.4 items/min (last 50)`
   and `3 flags · 2 pending notes`. **My** stream, not corpus coverage: with
   `targets_per_instance: 2` a global bar is a number the annotator cannot move.
2. **My answer mix (today).** Per class, a small bar with the annotator's own
   running share and the published expected band, e.g.
   `cannot tell 6 % (expected 3–9 %) ✓`. Anki's manual literally states
   "You'll typically use this button about 5-20% of the time"; drift is invisible
   to the person causing it unless it is rendered. **This block is here and not
   on the tile faces — see §6.1 for why.**
3. **Definition slot, reserved 240 px.** Empty at rest with a one-line
   placeholder. Fills when an info dot is clicked. Fixed height, so opening a
   definition **never moves a tile**.
4. **Recent (last 10).** Ten 40 px crop thumbnails, clickable, newest first;
   ArrowLeft walks this list so stepping back is aimed rather than blind. This is
   Prodigy's "ten most recent decisions … remain editable" sidebar, and it is what
   makes a Save-less flow feel safe. **Thumbnails only — no chosen classes.**
   See §6.2.
5. **Keys.** The permanent legend: `1–9 answer · ← back · → next · F flag ·
   Space inspect · Esc cancel`.

### 2.6 ASCII wireframe — 1440 × 900, Q1 active, fresh item

```
┌ 1440 ─────────────────────────────────────────────────────────────────────────────────┐
│ ▓ cownting   Dashboard   Data   Label   Manual                          mark ▾    73px │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ #16181A page. The crop is the brightest region on screen.                              │
│  solar-a · cam03 · 2026-05-14 09:41                                              28px  │
│  ┌ main 760 ────────────────────────────────────┐   ┌ side 320 ────────────────────┐   │
│  │ ┌ crop  C = 440 sq, centred ───────────────┐ │   │ MY STREAM                    │   │
│  │ │▒▒▒▒▒▒▒▒▒▒▒▒▒▒ scrim 45 % ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒│ │   │ 412 / 1 400 today            │   │
│  │ │▒▒▒▒▒▒▒▒▒▒▒▒╭───────────────╮▒▒▒▒▒▒▒▒▒▒▒▒│ │   │ ▓▓▓▓▓▓▓▓░░░░░░░░░░░░  29 %    │   │
│  │ │▒▒▒▒▒▒▒▒▒▒▒▒│   ringed cow  │▒▒▒▒▒▒▒▒▒▒▒▒│ │   │ 11.4 items/min (last 50)     │   │
│  │ │▒▒▒▒▒▒▒▒▒▒▒▒│  (full bright)│▒▒▒▒▒▒▒▒▒▒▒▒│ │   │ 3 flags · 2 pending notes    │   │
│  │ │▒▒▒▒▒▒▒▒▒▒▒▒╰───────────────╯▒▒▒▒▒▒▒▒▒▒▒▒│ │   ├──────────────────────────────┤   │
│  │ │▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒│ │   │ MY ANSWER MIX (today)        │   │
│  │ └──────────────────────────────────────────┘ │   │ shaded      ▮▮▮▮▮▮▯▯▯  38 %  │   │
│  │  ↕ 8px                                        │   │ direct sun  ▮▮▮▮▮▯▯▯▯  31 %  │   │
│  │ ┏ #2A2418 field · 3px #E0A03C left border ══┓ │   │ not visible ▮▮▮▯▯▯▯▯▯  25 %  │   │
│  │ ┃ ◣ SUN EXPOSURE                  [1/2] ●○  ┃ │   │ cannot tell ▮▯▯▯▯▯▯▯▯   6 %  │   │
│  │ ┃                                            ┃ │   │              expected 3–9 % ✓│   │
│  │ ┃ ┌────────┐┌────────┐┌────────┐┌────────┐  ┃ │   ├──────────────────────────────┤   │
│  │ ┃ │①     ⓘ││②     ⓘ││③     ⓘ││④     ⓘ│  ┃ │   │ DEFINITION            240px  │   │
│  │ ┃ │   ◣    ││   ☀    ││   ⦸    ││   ?    │  ┃ │   │ click ⓘ on an option to see  │   │
│  │ ┃ │ Shaded ││ Direct ││  Not   ││ Cannot │  ┃ │   │ its definition and two       │   │
│  │ ┃ │        ││  sun   ││visible ││  tell  │  ┃ │   │ example crops.               │   │
│  │ ┃ └────────┘└────────┘└────────┘└────────┘  ┃ │   │                              │   │
│  │ ┗════════════════════════════════════════════┛ │   ├──────────────────────────────┤   │
│  │  F flag · ← back · → next · hold Space inspect │   │ RECENT                       │   │
│  │  ● synced                                      │   │ ▫ ▫ ▫ ▫ ▫ ▫ ▫ ▫ ▫ ▫          │   │
│  └────────────────────────────────────────────────┘   ├──────────────────────────────┤   │
│                                                        │ KEYS 1–9 answer · ← back ·   │   │
│                                                        │ → next · F flag · Space      │   │
│                                                        │ inspect · Esc cancel         │   │
│                                                        └──────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.7 The same screen 140 ms later — Q2 active

Only the panel changed. The crop did not move, the panel did not move, the tile
row occupies **the same rectangle**. Q2 is never rendered greyed-out below Q1;
that is still a form.

```
│  │ ┏ #152526 field · 3px #3FB8AE left border ══┓ │
│  │ ┃ ⌐ BEHAVIOUR                     [2/2] ●●  ┃ │
│  │ ┃ ┌───────┐┌───────┐┌───────┐┌───────┐┌────┐┃ │
│  │ ┃ │①    ⓘ││②    ⓘ││③    ⓘ││④    ⓘ││⑤  ⓘ│┃ │
│  │ ┃ │  ⩗    ││  ▭    ││  ⌸    ││  ⊢▯   ││ ?  │┃ │
│  │ ┃ │Feeding││ Lying ││Standing││ Head  ││Can-│┃ │
│  │ ┃ │       ││       ││       ││probing││not │┃ │
│  │ ┃ └───────┘└───────┘└───────┘└───────┘└────┘┃ │
│  │ ┗════════════════════════════════════════════┛ │
```

### 2.8 A revisited item (ArrowLeft) — visibly different

```
│  │ ┏ #152526 field · 3px #3FB8AE · 2px dashed outline ═┓ │
│  │ ┃ ⌐ BEHAVIOUR   ✎ REVIEWING          [2/2] ●●      ┃ │
│  │ ┃ ┌───────┐┌───────┐┌───────┐┌───────┐┌────┐      ┃ │
│  │ ┃ │①    ⓘ││②  ✓ ⓘ││③    ⓘ││④    ⓘ││⑤ ⓘ│      ┃ │
│  │ ┃ │  ⩗    ││ ▓▭▓   ││  ⌸    ││  ⊢▯   ││ ?  │      ┃ │
│  │ ┃ │Feeding││ Lying ││Standing││ Head  ││Can-│      ┃ │
│  │ ┃ └───────┘└═══════┘└───────┘└───────┘└────┘      ┃ │
│  │ ┗═══════════════════════════════════════════════════┛ │
│  │  ✎ reviewing — → to leave · answers already saved      │
```

The chosen tile is filled, inverted, and carries a `✓` in the badge row. The
panel carries a dashed outline and the word `REVIEWING`. Encord, Roboflow and
Labelbox all give review its own visibly distinct mode; going back must never
look like a fresh item.

---

## 3. Moment-to-moment interaction

### 3.1 Item lifecycle

```
present → Q1 active → [digit|click] → latch 130ms → handoff 140ms → Q2 active
       → [digit|click] → latch 130ms → commit(local) → crossfade 100ms → present next
```

Total from the second keypress to the next crop being answerable: **230 ms**. No
network is on this path.

### 3.2 On item presentation

1. Client emits a `presented` event (§8.2) with the wall clock and the item key.
   This is the start of the per-item clock, replacing the batch-wide `served_at`.
2. `q = 0` (Sun exposure). Panel field cross-fades to `--lbl-q1-field` in 0 ms —
   it is already that colour, because the previous item ended by resetting it.
3. Digit badges `1..n` are stamped on the visible classes of the active group in
   **display order, left to right** — Label Studio's rule that auto-assigned
   hotkeys follow visible order. `numberKeysFor()` already produces exactly this
   and must remain the single source for both the badge and the handler.
4. Space-hold scrim state resets to 0.45 and crop scale resets to 1.

### 3.3 On a digit press (1..n of the active group)

| condition | behaviour |
|---|---|
| digit > option count | **nothing**, and that is a bug risk (CVAT issue #8400: keys 0–8 worked, key 9 silently no-op'd in v2.17.0). Render a 120 ms 4 px horizontal shake of the tile row so a dead key is *seen*. |
| digit selects the class already chosen for this group | **clears it**. Tile un-fills, group returns to unanswered, no advance. This is SuperAnnotate's and Supervisely's press-again-to-clear (`Toggle Tag on Hotkey`), shipped independently by two platforms, and it is the correction path that never leaves the item. |
| digit selects a different class | proceed to §3.4 |

### 3.4 The latch, the handoff, the advance

**Latch — 0 ms to 130 ms.** In the *same frame* as the keydown the tile inverts:
background → the question accent at full saturation, glyph and label → near-black,
2 px inset ring, a `✓` appears in the badge row. Any previously chosen tile in
this group un-fills in the same frame. This is non-negotiable: a keypress with no
visible state change in the same frame is the CVAT #8400 failure, and in a study
where honest answers matter it is worse than a slow UI. The latch is held 130 ms
so a *wrong* tile is seen rather than inferred.

An `answered` event is emitted immediately (§8.2) carrying
`{group_key, class_key, ms_since_group_shown, ms_since_item_shown,
replaced_class_key, input_mode}`.

**Handoff — 130 ms to 270 ms, when an unanswered group remains.** 140 ms total:

- Panel field cross-fades `--lbl-q1-field` → `--lbl-q2-field`, and the left
  border `--lbl-q1` → `--lbl-q2`, over the full 140 ms.
- Outgoing tiles: `opacity 1→0`, `translateY 0→-6px`, 0–90 ms, `ease-out`.
- Incoming tiles: `opacity 0→1`, `translateY 6px→0`, 50–140 ms, `ease-out`.
- **Digit badges animate last**: 60 ms delay, 100 ms, `opacity 0→1` +
  `scale 0.7→1`. The badges are the *only* thing telling the annotator what `2`
  now means, so the re-stamping must be seen, not discovered by error.
- Prompt row text and glyph swap; counter flips `[1/2]` → `[2/2]`; pip `●○` → `●●`.
- `prefers-reduced-motion: reduce` → opacity only, 80 ms, no translate, no scale.
  The badge delay stays; it is information, not decoration.

The panel's height is fixed at 108 px + padding regardless of tile count, so
nothing below it moves.

**Advance — 130 ms to 230 ms, when this was the last unanswered group AND the
item is `fresh`.**

- The answer is committed to the **local queue** and the item is marked done.
  The UI does not wait for the network (§3.5).
- The crop cross-fades to the next instance over 100 ms (`InstanceCrop`'s
  `key={crop_url}` already guarantees a stale cow is never answerable; the
  cross-fade is a `<div>` opacity transition on the wrapper, not on the image).
- Panel resets to Q1: field back to `--lbl-q1-field`, badges re-stamped, counter
  `[1/2]`. This reset is instant, under the crop cross-fade, so the annotator
  never sees a half-swapped panel.
- The "Recent" strip prepends the item's thumbnail.

**Advance is bound to the selection event only.** It never fires from an effect
watching `answers`, never from a timer, and never on a revisited item. The
documented practitioner failure with auto-advance is re-triggering on navigation
back to an already-answered item, which would make ArrowLeft — the only recovery
mechanism left — unusable. Anki shipped time-based auto-advance in 23.12 and
still requires a non-zero timer *and* a per-session menu activation; we ship no
timed advance at all.

**Advance is suppressed in `review` phase.** Answering the last group on a
revisited item latches, emits `answered`, updates the local queue, shows a
`saved ✓` micro-chip for 800 ms in the footer, and **stays**. The annotator
leaves with ArrowRight.

### 3.5 Auto-save, which is never a wait

Auto-save is an **optimistic local queue**, not a synchronous POST.

- Every answer mutates in-memory state and enqueues a write. The keyboard is
  never gated: `applyAnswer` loses its `busy` guard entirely. If a flush is in
  flight, the next answer is appended to the queue.
- The flusher posts in batches of ≤ 8 (matching `annotation.batch_size`) with
  exponential backoff on failure, 250 ms / 1 s / 4 s / 15 s, indefinitely.
- The last **10** answered instances stay in memory with their answers, so
  ArrowLeft re-shows and re-edits them with no round trip (Prodigy's
  `history_length: 10`).
- Save happens on every answer **event**, not on an interval. Encord's 15–600 s
  (default 120 s) and CVAT's 15-minute auto-save can each lose an interval of
  work; there is a natural unambiguous commit moment here, so use it.
- **Sync indicator**, 12 px, footer-right: filled dot = everything flushed;
  hollow dot with a count = *n* pending; hollow dot with a slow 1.2 s pulse =
  retrying. It is never a button and never blocks.
- On `beforeunload` with a non-empty queue: `event.preventDefault()` plus a
  `navigator.sendBeacon` attempt. The queue is also mirrored to
  `sessionStorage` on every mutation and replayed on mount.

### 3.6 Mouse

- **Click on a tile face** = the digit. Same latch, same handoff, same advance,
  `input_mode: "mouse"` on the event.
- **Click on the info dot** (24 × 24 hit box, top-right corner of the tile,
  opposite the badge, ≥ 10 px of clear space from the tile's own edge) opens that
  class's definition in the **reserved side-panel slot**. It never answers, never
  reflows, never covers the crop. The dot is a `<button>` that is a *sibling* of
  the tile button, not a child — nesting would make reading a definition answer
  the question. An `info_opened` event is emitted (the kind already exists).
- Tiles are `<button type="button" aria-pressed>`, **not** `<input type="radio">`
  in a `role="radiogroup"`. See §3.8.
- Click on a "Recent" thumbnail = jump to that item in `review` phase.

### 3.7 The other keys

| key | fresh item | revisited item |
|---|---|---|
| `ArrowRight` | if both groups answered: commit and go to the next item. If not: **no-op with a 120 ms shake** and an 11 px footer line `answer both questions, or press F to flag`. There is no skipping. | leave review, return to the head of the tape |
| `ArrowLeft` | step back one item; phase → `review`; active group resets to Q1 so the annotator re-reads from the top; any unanswered group is marked (§5.4) | step back another item |
| `F` | §3.9 | §3.9 |
| `Space` (hold) | scrim → 0, crop scales to fill the column, for as long as held | same |
| `Escape` | closes the flag reason row / clears the definition slot | same |
| everything else | unbound. **`?` is deliberately not bound at all** — decision 5 removes the overlay, and a `?` that does something unexpected is worse than a `?` that does nothing, because `?` means "shortcut help" across Gmail and GitHub. | |

### 3.8 Arrow-key ownership — the one hard bug to not ship

`LabelGroup.tsx:162` currently sets `role="radiogroup"` with `sr-only`
`<input type="radio">` children. A native radiogroup moves the checked radio on
Left/Right/Up/Down and fires `onChange`. With unconditional auto-save, **one
mouse click on an option arms ArrowLeft to write a label instead of navigating**.
`labelKeys.ts:168` exempting `radio` from the typing guard does not help: only
`preventDefault` stops the native behaviour, and the window listener runs at
bubble phase, after the input has already acted.

Fix, both halves:

1. Drop the radiogroup. Tiles become `<button type="button" aria-pressed>` inside
   a `<div role="group" aria-labelledby="...">` with a single roving `tabindex`.
   Buttons have no native arrow behaviour.
2. Register the page key handler on `window` in the **capture** phase and call
   `preventDefault()` on `ArrowLeft`/`ArrowRight`/`Space` before anything else
   sees them.

Label Studio uses `Ctrl+Left`/`Ctrl+Right` for image navigation *specifically*
because bare arrows are consumed by region editing; Roboflow degraded
Classification navigation to `Cmd+Left`/`Cmd+Right` after spending Up/Down on
class options. We keep bare arrows, which is strictly better at 2,800 items — and
the price is that arrows must never, ever be bound to options "as a convenience".

**Regression test, checked in:** focus an option tile with the mouse, press
ArrowLeft, assert the instance changed and `answers` did not.

### 3.9 FLAG — cheap to reach, still fully justified

Decision 6 requires a reason *and* a written explanation. The research says make
`F` mark-and-move and defer both; the skeptic says the five reasons
(`bad_crop`, `no_cow`, `multiple_cows`, `occluded`, `other`) are pixel-level
distinctions that cannot be reconstructed 300 items later, and that
`multiple_cows` in particular is a direct detector-quality signal
(`labels_db.py:228-230`).

**We take the skeptic's side and split the two halves.** Reason at the pixels;
prose from the thumbnail.

1. `F` swaps the **tile row in place** to the five reasons, bound to digits 1–5,
   same rectangle, same badges, same rhythm — so it is not a task switch. (A
   558-participant AMT study found sequences of distinct task types measurably
   hurt classification engagement and accuracy; a modal over the crop is exactly
   such a switch.) Prompt row reads `⚑ FLAG — why?` on the neutral alarm field.
2. A digit writes the flag **with its reason** and advances, at the same 130 ms
   latch + 100 ms cross-fade as a normal answer. Two keystrokes total.
3. The item is appended to a **pending-notes queue** with its crop thumbnail. The
   side panel shows `n pending notes`; ArrowRight past the end of the day's
   stream, or a click on that count, opens the queue as a full-page list —
   thumbnail, reason, textarea, one per row. The session cannot be marked
   complete with the queue non-empty.
4. `Escape` during step 1 returns to the question, unchanged.

This keeps the mandatory reason-plus-explanation intact while taking the flag off
the per-item critical path. Prodigy's flag is a bare `f` bookmark with no
justification and is off by default; ours is stricter than any platform surveyed,
and the cost belongs at end of session, not at 15–30 s against a 3 s item.

---

## 4. The icon set

**Design rule.** Options must separate on **one gross pre-attentive dimension
each** — silhouette, mass distribution, orientation. Single-feature search runs
in parallel (< 200–250 ms); a *conjunction* of features collapses to serial
search, at which point the icon is just a slower word. "Which line is inside the
little glyph" is a conjunction. Subject matter is not a pre-attentive feature.

**Rendering rules.**

- 24 × 24 canvas, rendered at **26 px** in tiles (never below 24 — Heroicons
  ships no 20 px *outline* set at all, only 20 px and 16 px **solid**, which is a
  professional icon team's verdict that 2 px strokes stop resolving below 24).
- `stroke="currentColor"`, `stroke-width="1.75"` on the dark surface. Thinner
  than 2 because light-on-dark strokes bloom; Material Symbols exposes a `grade`
  axis and suggests −50 for reversed contrast for exactly this.
- `stroke-linecap="round"`, `stroke-linejoin="round"`, `fill="none"` by default;
  the two fills below declare `fill="currentColor" stroke="none"` locally.
- All nine live in `frontend/src/components/ClassIcon.tsx`, inline JSX, no sprite
  sheet, no font, no `data:` URI — satisfying the strict CSP with zero config and
  staying themeable via `currentColor` so selection state is pure CSS.
- A `THIRD_PARTY_LICENSES` header retains the ISC (Lucide) and MIT (Phosphor)
  notices for the glyphs derived from them.

Nine shipped classes, **eight distinct glyphs**: `question` is deliberately
reused for both `Cannot tell` classes so it becomes a question-independent
"I don't know" landmark, and `eye-off` will be reused for
`behaviour.not_visible` when it lands (§6.3).

### 4.1 Sun exposure — coded by shape primitive

| class | glyph | primitive, and why it is unique in the set |
|---|---|---|
| Shaded | `shade` | **solid diagonal slab** — the only filled diagonal mass |
| Direct sun | `sun` | **radial starburst with a filled core** — the only radially symmetric glyph |
| Not visible | `eye-off` | **full-frame slash** — the only corner-to-corner single line |
| Cannot tell | `question` | **full-frame closed ring** — the only closed outline touching the frame |

**`shade` — Shaded.** A thick solid bar running corner to corner at ~25° across
the middle, with a partial sun disc emerging above it and clean empty space
below. Domain-literal: in a solar field the shade *is* a panel edge. Rays are
deliberately omitted — the slab carries the meaning and 1.6-unit rays are 1.3 px
at 20 px, i.e. ink with no signal.

```jsx
shade: (
  <>
    <path d="M4.2 11.2a3.8 3.8 0 0 1 7.6 0" />
    <path d="M1.6 15.4 22.4 6.0 22.4 10.6 1.6 20.0 Z"
          fill="currentColor" stroke="none" />
  </>
)
```

**`sun` — Direct sun.** Filled centre disc plus eight straight rays. The centre
is **filled**, not hollow, so it holds at 20 px; this is the single change that
separates it from `shade` under the Lucide `sun`/`sun-dim` failure mode.

```jsx
sun: (
  <>
    <circle cx="12" cy="12" r="4.2" fill="currentColor" stroke="none" />
    <path d="M12 1.5v3.2M12 19.3v3.2M1.5 12h3.2M19.3 12h3.2" />
    <path d="M4.6 4.6 6.9 6.9M17.1 17.1 19.4 19.4M19.4 4.6 17.1 6.9M6.9 17.1 4.6 19.4" />
  </>
)
```

`shade` and `sun` now share **no** structural element: one is a filled
lower-left-heavy diagonal mass, the other is a filled centred disc with radial
symmetry. That is a mass-and-orientation difference, resolvable in parallel.

**`eye-off` — Not visible.** Simplified from the current four-path version so it
survives 20 px; the slash is the load-bearing element.

```jsx
"eye-off": (
  <>
    <path d="M3.2 12s3.6-6.2 8.8-6.2S20.8 12 20.8 12s-3.6 6.2-8.8 6.2S3.2 12 3.2 12Z" />
    <circle cx="12" cy="12" r="2.6" />
    <path d="M2 2 22 22" />
  </>
)
```

`eye-off` and `shade` both contain a diagonal, so they are separated by **mass**,
not angle: `shade`'s diagonal is a filled quad, `eye-off`'s is a 1.75 px line.

**`question` — Cannot tell.** Unchanged from the current implementation; it is
already correct. Ring radius raised to 9.5 so it reads as full-frame.

```jsx
question: (
  <>
    <circle cx="12" cy="12" r="9.5" />
    <path d="M9.3 9.2a2.8 2.8 0 1 1 3.8 2.7c-.8.35-1.1 1-1.1 1.75v.45" />
    <path d="M12 17.6h.01" />
  </>
)
```

### 4.2 Behaviour — coded by position relative to a shared ground line

All four postural glyphs carry an **identical 1.75 px ground line at y = 20 from
x = 2 to x = 22**. The body then differs only in its position and orientation
relative to that line. This turns four separate pictures into one comparison —
*where is the animal relative to the line, and where is its head* — which is what
makes the glyphs faster than the words.

| class | silhouette | discriminator |
|---|---|---|
| Feeding | bottom-left heavy, descending diagonal, muzzle **touching** the line | head down |
| Lying | wide, low, **flush on** the line, zero clearance | no leg gap |
| Standing | raised, **visible negative space under** the body | leg gap |
| Head probing | body raised, neck thrust **horizontally into a bracket** at the frame edge | horizontal spike |
| Cannot tell | the same full-frame ring as Q1 | identical to Q1 |

`Lying` and `Standing` differ by exactly one thing — the presence or absence of
the leg gap — which is a pure figure/ground difference and survives to 16 px. It
is also precisely the discriminator the seeded definitions already give
annotators (`labels_db.py:743-753`).

```jsx
// Feeding — head down, muzzle on the line, two blades at the muzzle.
grass: (
  <>
    <path d="M2 20h20" />
    <rect x="11" y="8.2" width="9" height="6" rx="3" />
    <path d="M11.4 10.6 5.6 18.8" />
    <path d="M13.6 14.2V20M18 14.2V20" />
    <path d="M3.4 20c0-1.9.5-3 1.6-3.9M7.4 20c0-1.9-.5-3-1.6-3.9" />
  </>
),

// Lying — body flush ON the line, no leg gap, head raised slightly.
lying: (
  <>
    <path d="M2 20h20" />
    <rect x="3.6" y="14.4" width="12.4" height="5.6" rx="2.8" />
    <path d="M15.6 16.4 18.6 13.6" />
    <circle cx="19.8" cy="12.2" r="1.8" />
  </>
),

// Standing — same body raised, 6.6 units of clear space under it.
standing: (
  <>
    <path d="M2 20h20" />
    <rect x="4.4" y="8" width="11.6" height="5.4" rx="2.6" />
    <path d="M6.8 13.4V20M13.6 13.4V20" />
    <path d="M15.6 9.6 18.6 7" />
    <circle cx="19.8" cy="5.6" r="1.8" />
  </>
),

// Head probing — standing body, neck HORIZONTAL, crossing into an open bracket
// at the right frame edge. Reaching THROUGH something is the class; the bracket
// carries it. The current `probe` glyph is a downward arrow between two dashes —
// i.e. the download icon — and carries no ground line, breaking the family.
probe: (
  <>
    <path d="M2 20h20" />
    <rect x="2.6" y="8.4" width="9.4" height="5" rx="2.5" />
    <path d="M4.8 13.4V20M10 13.4V20" />
    <path d="M12 10.4h6.2" />
    <path d="M17.2 6.2h4.4v8h-4.4" />
  </>
),
```

`standing` vs `probe` at 20 px: both are body + legs + ground line; the
differentiator is **neck orientation** (diagonal-up-to-a-circle vs
horizontal-into-a-bracket). Orientation is on the parallel-detectable list, so
this is a single-feature difference, not a conjunction.

`dot` is unchanged — the neutral fallback for a class an operator has not given
an icon, so a taxonomy edit degrades to a bullet rather than to a hole in the
tile grid.

### 4.3 Adoption gate — the icons must be *learned*, not guessed

A guessed icon is a **systematic** error: every annotator guesses the same wrong
way, so agreement rises while the labels are collectively wrong. That is the one
failure direction that looks like success.

Ship a **20-item forced-choice icon→name test at onboarding**, stored per
annotator: the glyph alone at 26 px, four names, no context. Gate production
labeling on ≥ 18/20, and store the per-glyph error matrix. If `shade`/`sun` or
`grass`/`probe` account for the misses, the glyph is wrong, not the annotator.
"Learned" is checkable; "guessed" is not, and shipping without the test means we
will never know which we got.

---

## 5. Colour and state

**Constraint first**: WCAG 2.2 SC 1.4.1 — colour is never the only visual means
of conveying information. Every state below carries **at least two channels**.
No red/green pairing anywhere. The whole set must be verified in greyscale, and
the greyscale render is the acceptance artefact, not a courtesy.

### 5.1 Which question is active

| channel | Sun exposure | Behaviour |
|---|---|---|
| panel field | `#2A2418` warm | `#152526` cool |
| 3 px left border | `#E0A03C` | `#3FB8AE` |
| prompt glyph | `shade` | `probe` |
| prompt text | `SUN EXPOSURE` | `BEHAVIOUR` |
| counter | `[1/2]` | `[2/2]` |
| step pip | `●○` | `●●` |
| tile count | 4 | 5 (6 after §6.3) |

Seven channels, of which only two are colour. Amber vs teal is on the blue-yellow
axis and survives both protanopia and deuteranopia; the two fields also differ in
lightness (L\* 15 vs L\* 13 is not enough on its own, which is why the other five
channels exist). The counter and the pip are the load-bearing cues.

This matters more than anything else in this section. Key `2` means *Direct sun*
in one state and *Lying* in the other — a varied mapping across contexts. A weak
visual difference between the two states is the **worst** case, because it
invites the wrong overlearned response and produces a valid class, no error, and
an auto-advance.

### 5.2 The current answer

- Tile background → the active question's accent at full saturation.
- Glyph and label → `#16181A` (near-black on accent), i.e. **inverted**.
- 2 px inset ring in the accent.
- A `✓` appears in the badge row.

Four channels: fill, inversion, ring, glyph. Works in greyscale (the fill is far
lighter than `--lbl-tile`).

### 5.3 Answered vs unanswered question

On a **fresh** item the question you are not on does not exist on screen, so
there is nothing to indicate. On a **revisited** item:

- Panel gains a 2 px dashed outline and the prompt row gains `✎ REVIEWING`.
- Each answered group's chosen tile is filled per §5.2.
- Any **unanswered** group is marked with a `✽` asterisk before its prompt text
  plus the word `unanswered` in the prompt row — V7's "required properties are
  easily identifiable by a red star next to their names", implemented as
  shape + word rather than as colour.

### 5.4 Flagged

Flag state must not read as a sun class, so it uses **no hue at all**:

- 3 px `#C8CDD2` outline around the crop, drawn as a 6/4 dash.
- A 45° 4 px hatch band across the top 20 px of the crop wrapper.
- The word `FLAGGED` plus the flag glyph and the reason, 12 px, in the footer.

Three channels, zero colour dependence.

### 5.5 Sync

Filled dot = flushed. Hollow dot + count = pending. Hollow dot + 1.2 s pulse =
retrying. Shape and motion, no colour.

---

## 6. Quality guards

Every mechanism identified in the skeptic pass moves **measured kappa upward**:
escape-inflation, retro-fitting on revisit, a shared prime, guessed-but-consistent
icons. There is no mechanism in this design that makes bad data look bad. These
guards exist to supply one.

Note what we do **not** claim: "auto-advance harms accuracy" is folklore, and so
is its denial — no peer-reviewed study measures it in surveys or in annotation,
and both positions trace to vendor marketing. We defend the mitigation stack
instead, every element of which is evidenced.

### 6.1 Per-decision telemetry, without which nothing else here works

`EVENT_KINDS` (`labels_db.py:246-249`) has no per-answer event, and
`annotation_choices` (`labels_db.py:424-432`) has no timestamp. Today a mis-key
and a considered answer are byte-identical in the store.

**Add two event kinds:**

- `presented` — emitted by the client when an item actually becomes the current
  item. This is the real per-item clock. The existing `served` row is written once
  per **batch** at fetch time (`labeling.py:412-424`), so with `batch_size: 8`
  item *k* carries the elapsed time of items 1..*k*−1 as well as its own —
  `time_on_task_ms` is inflated by a mean of ~3.5 item-durations. Keep the batch
  `served` row (it is the correct abandonment denominator for `SQL_ABANDONED`),
  but compute effort from `presented → answered`.
- `answered` — one per digit press or tile click, **including presses that
  overwrite an earlier answer on the same item**, with detail
  `{group_key, class_key, ms_since_group_shown, ms_since_item_shown,
  replaced_class_key, input_mode}`.

That single addition yields per-question response time, per-question RTE, and a
directly countable within-item correction rate. Also emit the already-declared
`relabel` kind on every in-place change made in `review` phase, carrying the old
class, the new class, and how many items back the annotator had travelled.

**Where the research and the skeptic disagree, and what we chose.** The research
says to print the annotator's running share for each class **on the tile face**,
after Anki, which prints each button's next review interval on the button. We put
it in the **side panel instead**. Anki can do it because the interval is a
*consequence* of the choice, not a prior over it; a live percentage on the tile at
decision time is a nudge that operates on the decision in front of you, and
annotators will balance toward the published band. That manufactures a
distribution rather than measuring one — the same class of error as a sticky
default. The side panel keeps drift visible without putting it in the fixation
region at the moment of choice.

### 6.2 The revisit must not become harmonisation

`v_current_answers` (`labels_db.py:547-554`) filters `superseded_at IS NULL`, and
`_ANS_CTE` (`labels_db.py:1846-1852`) — the entry point for every agreement query
— starts from it. So an annotator who walks back three items to "make the run
consistent" produces a corpus in which only the harmonised answers exist, and the
direction is always upward. The version-1 rows survive (`undo_last` flips
`outcome` and sets `superseded_at`, never deletes); nothing reads them.

Three guards:

1. **Report first-answer kappa alongside the headline.** Add a variant of
   `_ANS_CTE` that drops the `superseded_at` filter, adds `AND a.version = 1`, and
   joins `annotation_choices` directly on `annotations`. The gap between
   first-answer kappa and headline kappa **is** the retro-fit measurement. One
   more SQL constant next to the eight already at `labels_db.py:1967-1974`.
2. **The Recent strip shows thumbnails only — never the chosen classes.** The
   research recommends showing the two chosen icons per row, after Prodigy's
   sidebar. We take the skeptic's side: a visible list of what you just answered
   is precisely what turns correction into harmonisation, and this study's output
   is agreement. Thumbnails give ArrowLeft an aimed target without displaying the
   answers.
3. **Review phase is visibly different and never auto-advances** (§2.8, §3.4).

### 6.3 `behaviour.not_visible` — before the first production day

Sun exposure ships `not_visible` (`is_escape: False`) *and* `cannot_tell`
(`is_escape: True`), and its definition draws the line cleanly: "This is for a
physical or optical obstruction. If you can see the body fine but still cannot
decide sun vs shade, that is Cannot tell, not this." Behaviour ships only
`cannot_tell`, and its definition instructs annotators to use it for *both*
occlusion **and** "Feeding vs Head probing is a coin flip". One class, two
meanings.

`Not visible` is a **fact about the image**; `Cannot tell` is a **confession
about the annotator**, and only the second is what Krosnick's satisficing warning
is about. Without parity, occlusion contaminates the escape class, the
`SQL_*_NO_ESCAPE` sensitivity reading (`labels_db.py:1968-1974`) drops two
different things in the two questions, and the escape-rate monitor below becomes
uninterpretable for Behaviour.

Add `behaviour.not_visible`, `icon: "eye-off"`, `is_escape: False`,
`sort_order: 45` — one `create_class` call plus a seed entry. Behaviour becomes
six tiles: 6 × 112 + 5 × 10 = 722 px, still one row inside 760, still inside the
1..9 budget (Prodigy documents a nine-option ceiling for digit selection; CVAT
hard-codes ten via `Array.from({ length: 10 })`).

**Do it before 2,800 items exist.** `taxonomy_revision` is stamped on every
annotation (`labels_db.py:602-609`) so a mid-campaign change is detectable, but a
corpus with two taxonomies is a limitation we would rather not write.

### 6.4 Escape-rate monitoring

With Skip abolished and Flag moved off the critical path, `Cannot tell` is the
cheapest key on the screen — and two raters both taking the cheap key **agree**,
so escape-inflation raises kappa while quality falls. Snapshot Serengeti shipped
no "I don't know" at all and bought 96.6% expert agreement with 10–25
classifications per item; our redundancy is `targets_per_instance: 2` with
`overlap_targets: 3` on a 20% slice. We cannot lean on consensus the way that
study did, so we lean on instrumentation.

Ship, on the same query surface as `SQL_EFFORT_BY_ANNOTATOR`
(`labels_db.py:1978-1995`), a per-annotator × per-session × per-group escape rate
and its median latency. It is a `count(*) FILTER (WHERE c.is_escape)` over
`v_current_answers` joined to `label_classes`, which `_agreement_sql` already
knows how to join (`labels_db.py:1955-1960`).

**The diagnostic that separates the two hypotheses**: if median latency on the
escape class is roughly equal to median latency on the real classes, it is being
used as a skip; if it is materially longer, it is a genuine hard-case call. That
test is only computable once §6.1 lands, which is why §6.1 is a prerequisite and
not a nice-to-have.

**Do not remove the escape classes.** Krosnick's finding is about attitude
questions where the respondent's own opinion is the ground truth, the wider
literature is genuinely mixed, and ethology has carried "out of sight" as a
standard bookkeeping category since Altmann (1974). The seeded definitions are
right that a forced guess is noise. Monitor the rate; keep the option.

### 6.5 Response Time Effort

Wise & Kong (2005) classify each response as solution behaviour or rapid guessing
against a per-item response-time threshold; RTE is the proportion of effortful
responses, and **RTE below 0.90 indicates meaningful disengagement**. Validation
rules: inspect the RT distribution visually, place the threshold in the trough of
its bimodal shape, and never let the threshold exceed 10 s.

With `answered` events this is nearly free. Compute RTE **per annotator, per
session, per group**. RTE dropping below 0.90 mid-session is objective grounds to
re-queue that annotator's last *N* items — far better than trusting a Save button
to imply deliberation.

### 6.6 Gold items at ~5%

There is no known-answer concept anywhere in `cownting/`, so every quality signal
is annotator-versus-annotator and "confidently, collectively wrong" is
structurally invisible — which is exactly the failure the PLOS ONE crowdworker
study documented ("erroneously annotated with high agreement and low
uncertainty"). Downstream QA cannot buy it back: the *Quality Assured* study
(57,648 masks, 924 annotators, 34 dedicated QA staff) measured internal QA
improvement at **below 2% relative**.

No new table. Gold instances are ordinary `instance_key`s carrying an expert
annotation already in `annotations`; the queue needs one branch that keeps them
eligible for an annotator who has already answered them, and excludes them from
the coverage target — a `gold` flag in `label_meta` or a small key list, plus an
exception to the `NOT s.mine_done` clause at `labeling.py:394-395`.

Rate: **~5%, roughly 1 in 21** — CIFAR-10H's shipped rate was 10 attention checks
per 210 trials. At 2,800 items/day that is ~140 gold items, enough to detect
within-session drift without meaningfully taxing throughput.

Gold accuracy plotted against minutes-into-session is the **only honest answer to
the fatigue question**. Do not import the classic vigilance numbers (10–15%
detection loss in the first half hour); those paradigms are rare-signal and
externally paced, and the medical crowdworking study found the opposite pattern
here — annotators got *faster* (−0.71 s/item) with almost no correctness change.
Do not cite the "45-minute maximum annotation session" figure at all; it appears
in vendor blogs with no traceable primary study.

### 6.7 Queue order — stop collecting every pair under fatigue

`labeling.py:408` orders `s.n_labeled ASC, md5(? || s.instance_key)`. Items with
zero labels sort strictly ahead of items with one, so two annotators each burn
through ~1,400 virgin items before either produces a single **pair**. 100% of the
data feeding `SQL_PAIRWISE_AGREEMENT`, `SQL_COHENS_KAPPA` and `SQL_FLEISS_KAPPA`
is therefore drawn from the back half of each annotator's workload, and kappa is
structurally confounded with minutes-into-session.

**Drop `s.n_labeled ASC` from the ORDER BY.** The WHERE clause already caps
coverage at target, so ordering by the per-annotator `md5(? || s.instance_key)`
alone still terminates, still self-consumes, and still gives each annotator a
different walk — but pairs then accumulate uniformly through the session, which
makes "kappa by minutes-into-session" a real cut. **Unrecoverable retroactively:
land this before the second annotator starts.**

### 6.8 Crop padding, measured rather than inherited

`crop_pad: 0.35` of the longer bbox side (`config.py:127`), with out-of-frame
padding filled at mid-grey (`labeling.py:59`), means an instance near the frame
edge gets a grey surround — and Sun exposure is judged from the surroundings.
Edge instances are therefore pushed toward `Not visible`/`Cannot tell` on Sun
exposure but not on Behaviour: a class-differential, position-dependent bias that
already exists, and that changing the crop size will move.

Log the **padded fraction per item** — computable from `crop_geometry`
(`labeling.py:567-607`), which already returns the source box and the ring — and
test escape rate against it. If `Not visible` on Sun exposure correlates with
padded fraction, the crop margin is producing labels rather than the cow. CVAT
exposes the equivalent as a named setting ("Attribute annotation mode (AAM) zoom
margin"), which is the precedent for treating it as one consciously chosen
constant. Validate it by measuring Sun-exposure agreement at two padding values on
a calibration batch.

### 6.9 Definitions that are pictures, not more prose

The Nature Machine Intelligence 2023 study (14,040 images, 156 professional
annotators, 708 crowdworkers, three instruction tiers) found that **including
exemplary images significantly boosts annotation performance while solely
extending text descriptions does not**. Our definitions are already excellent
operational prose in the style that got cattle-behaviour studies to κ = 0.84.

So the definition slot renders, in this order: the class name, the existing
one-to-two-sentence operational definition, then **a canonical positive example
crop and a near-miss negative example crop, 96 px each, side by side, captioned
`yes` and `no`**. Store them as two nullable `instance_key` columns on
`label_classes` so the taxonomy editor can pick them from real data.

Separately, keep a persistent **Field Guide** page (not a modal) listing all
classes with one example crop each, linked from the side panel.

### 6.10 Things not to do, stated so nobody adds them later

- **Never give one class an extra binding.** Anki gives *Good* three keys
  (3/Space/Enter) because it is legitimately 80–95% of answers; here a fast path
  on one class is a systematic bias generator that shows up directly in the
  marginals Cohen's *p*ₑ is computed from (`labels_db.py:1882-1894`).
- **Never reorder options** by frequency, recency, or model confidence.
  `labelKeys.ts:86-93` already prevents this; protect that property.
- **Never pre-fill an answer from the previous instance.** Encord's "Preserve
  chosen state" would inflate agreement artificially while corrupting the
  honest-answers goal.
- **Never put a non-answer function on 1..9.** Roboflow spends 0 and 1 on zoom
  and has no digits left for classes.
- **Never let either question exceed nine options.**
- **Run a joint calibration batch before the first production day.** Both video
  cattle-behaviour studies that reached κ = 0.84–0.95 did joint training plus a
  pilot annotated to harmonise interpretation first. Neither is a UI feature and
  neither can be substituted by one.
- **Measure the order prime once.** Sun-then-Behaviour is fixed and shared across
  raters, and a shared prime reads as agreement. Run the calibration batch twice
  over the same instances — once in the shipped order, once as two separate passes
  days apart — and report the delta as a stated limitation. Check
  `P(Behaviour | Sun)` against the separated pass: if `Head probing` is
  disproportionately more likely after `Shaded`, the prime is measured. This costs
  one small batch, not a UI change.

---

## 7. What to remove

**Delete outright**

| what | where | why |
|---|---|---|
| the Save button and its Enter binding | `Label.tsx` footer | selection **is** submission; Prodigy made `choice_auto_accept` the default in v1.11.0 for mutually-exclusive single-select |
| Skip, the skip dialog, the `S` key, `SkipDialog` | `Label.tsx:646` and the dialog component | decision 6 |
| both preference checkboxes (auto-submit, show-definitions) and their `localStorage` reads | `LabelProgress.tsx:182-193`, `Label.tsx:194` | decisions 4 and 5: there are no preferences at all |
| `toggleAllDefs` and any `I` binding | `Label.tsx:262` | decision 5; nine definitions inline is the wall of text the redesign exists to remove |
| `LabelGroup.tsx` in its entirety | — | replaced by `QuestionPanel.tsx` (one active question, tile grid) |
| the `sr-only` `<input type="radio">` and `role="radiogroup"` | `LabelGroup.tsx:107-162` | §3.8 — a native radiogroup can make ArrowLeft write a label |
| the inline definition block under each row | `LabelGroup.tsx:129-131` | moved to the reserved side-panel slot; opening a definition must not move a target |
| the right-floated `InfoButton` | `LabelGroup.tsx:73` | moves into the tile corner at a 24 px hit box with ≥ 10 px clearance |
| the `busy` guard in `applyAnswer` | `Label.tsx:569` | keypresses must never be dropped silently |
| the `await submitLabel()` on the advance path | `Label.tsx:456-485` | replaced by the optimistic queue |
| the corpus-coverage hero bar | `LabelProgress.tsx:128-146` | a number the annotator cannot move; replaced by *my stream* |
| `my_median_ms` as an all-time scalar | `LabelProgress.tsx:121` | replaced by a rolling items/min over the last 50 |
| `max-w-[440px]` and the near-white `Card` around the crop | `Label.tsx:830`, `ui.tsx:12-38` | §2.2, §2.3 — a light surround biases the variable Q1 measures |
| `?`, `H`, `U`, `Enter`, `Backspace` bindings | `labelKeys.ts` | already correct in `LABEL_ACTIONS`; keep it that way |

**Fix, don't delete**

- `Label.tsx:26-33` still imports `buildKeyMap`, `actionForEvent`,
  `optionForEvent`, `groupKeyHint` and `LabelKeyMap`, none of which exist in the
  rewritten `labelKeys.ts`. **The page does not compile as committed.** That is
  the right moment to land the telemetry (§6.1) and queue (§6.7) changes, because
  they touch the same write path and the same event vocabulary, and every one of
  them is cheaper now than after 2,800 items exist.
- `Label.tsx:589` auto-submit must become selection-event-bound and
  phase-aware (§3.4).
- `ClassIcon.tsx` default `className` moves from `w-4 h-4` to `w-[26px] h-[26px]`,
  `strokeWidth` to `1.75`, and the four glyphs in §4 are replaced.

**Add**

`QuestionPanel.tsx`, `OptionTile.tsx`, `FlagReasonRow.tsx`,
`PendingNotesQueue.tsx`, `RecentStrip.tsx`, `DefinitionSlot.tsx`,
`useAnswerQueue.ts` (optimistic flush), `useLabelTelemetry.ts`
(`presented`/`answered` emission).

---

## 8. Acceptance criteria

Write the measurement with the feature, not after it. `docs/roadmap/M3_labeling.md`
§9.1 already models this discipline by refusing to hardcode whether "Cannot tell"
is a category or missing data and shipping both SQL variants; extend the same
posture here.

### 8.1 The target, and where it comes from

| source | figure | what it is |
|---|---|---|
| Explosion / Prodigy (Tom Tom Founders Festival 2018 abstract) | 10–30 decisions per minute, i.e. **2–6 s per decision** | published throughput for single-decision annotation |
| Prodigy launch post case study | ~830 annotations in ~40 min ≈ **2.9 s/decision** | one real task, single decision |
| McDonald et al., *Academic Radiology* 2015;22(9):1191–98 | **3–4 s per image**, 8-hour day | trained specialists, single-modality visual task, no data entry (obtained via search summaries; the journal article is paywalled) |
| Thorpe, Fize & Marlot 1996, *Nature* 381:520–522 | ~92% correct at **390 ms** RT | the perceptual floor |
| arXiv 2412.00260 (30 participants, 250 images, 10 classes) | 88.32% @ 100 ms, **97.83% @ 1000 ms**, 95.98% @ 2500 ms | ~1 s of viewing suffices; 2.5 s was *worse* than 1 s |

**Our targets**, stated as extrapolation, not as citation:

- **Median per-decision time (`presented`→`answered` for Q1;
  `answered`(Q1)→`answered`(Q2) for Q2): 1.2–3.0 s. p90 ≤ 6 s.**
- **Median per-instance time (`presented`→ second `answered`): ≤ 4.0 s at steady
  state, stretch 3.0 s.** Steady state = after that annotator's first 200 items,
  because Mowbray & Rhoades (1959, through 45,000 trials) and Seibel (1963, 1,023
  alternatives) show the set-size cost vanishes with practice; option count is an
  onboarding cost, not a throughput cost.
- 2,800 instances at 4.0 s = **3.1 h**; at the current ~6.5 s = **5.1 h**. That
  two-hour gap per annotator per day of footage is the business case, and it is to
  be *checked*, not asserted.

### 8.2 What we measure it from

Existing columns: `served_at`, `submitted_at`, `time_on_task_ms`,
`client_elapsed_ms`, `input_mode` (`labels_db.py:405`, `:1280`, `:1379-1385`).

Two corrections are required before any of these mean what they say:

1. `served_at` is written **once per batch** (`labeling.py:412-424`), so
   `time_on_task_ms` for item *k* of a batch of 8 includes items 1..*k*−1 —
   inflated by a mean of ~3.5 item-durations. Keep it as the abandonment
   denominator for `SQL_ABANDONED`; do **not** use it for effort.
2. `client_elapsed_ms` (via `activeElapsedMs`, `Label.tsx:212`) is a
   visibility-aware active clock — correct, and the right tab-away detector — but
   it is **one number spanning both questions** (`Label.tsx:466`), so it cannot
   answer the question the whole redesign turns on.

So the acceptance queries read from the new `presented` and `answered` events
(§6.1), and cross-check against `client_elapsed_ms`. The
`server(presented→submitted)` minus `client_elapsed_ms` delta then means what the
comment at `labels_db.py:2012-2014` says it means.

### 8.3 The checked-in acceptance query set

Add to `labels_db.py` alongside the eight agreement constants at `:1967-1974`.
All are per annotator × per session unless stated.

| # | metric | pass condition |
|---|---|---|
| A1 | median Q1 decision time | 1.2–3.0 s |
| A2 | median Q2 decision time | 1.2–3.0 s **and ≤ 1.25 × A1** |
| A3 | median per-instance time, steady state | ≤ 4.0 s |
| A4 | p90 per-decision time | ≤ 6 s |
| A5 | Response Time Effort (§6.5) | ≥ 0.90; threshold in the RT trough, never > 10 s |
| A6 | escape rate per group | inside the published band (§6.4) |
| A7 | median escape latency ÷ median non-escape latency | ≥ 0.8 |
| A8 | within-item correction rate (`answered` with non-null `replaced_class_key`) | ≤ 3% |
| A9 | first-answer κ vs headline κ (§6.2) | \|Δ\| ≤ 0.05 |
| A10 | gold accuracy (§6.6) vs minutes-into-session | slope not significantly negative |
| A11 | headline Cohen's κ per group | ≥ 0.667 tentative, ≥ 0.80 firm; domain benchmark for these behaviour classes is **κ = 0.84** from video cattle-behaviour studies with a standardised ethogram and joint calibration |
| A12 | keyboard share (`input_mode`) at steady state | ≥ 95%; and median mouse-mode time **>** median keyboard-mode time — if mouse is faster, the tiles are winning over the digits and the badge/handoff design has failed |
| A13 | icon adoption test (§4.3) | ≥ 18/20 before production labeling |

**A2 is the sharpest test in the set.** If Behaviour is consistently slower than
Sun exposure by more than a quarter, either the behaviour glyphs are not being
spotted or the question order is wrong — and both are fixable, but only if
measured.

### 8.4 Layout regression tests

- **Playwright at 1366 × 768**: load the Label page, assert that at `scrollY === 0`
  the crop, the full tile row, and the footer flag hint are all inside the
  viewport, and that `document.documentElement.scrollHeight <= innerHeight`.
- **Playwright at 1920 × 1080 and 2560 × 1440**: same.
- **Arrow-ownership test** (§3.8): focus a tile with a click, press ArrowLeft,
  assert the instance changed and `answers` did not.
- **Handoff test**: answer Q1, assert within 300 ms that the tile row's bounding
  box is unchanged, the counter reads `[2/2]`, and the digit badges map to the
  Behaviour classes.
- **Revisit test**: ArrowLeft onto an answered item, change Q1, assert the
  instance did **not** change.
- **Greyscale render**: snapshot the tile row with `filter: grayscale(1)` for both
  questions and both selection states; all nine glyphs must remain mutually
  distinguishable in the snapshot.

### 8.5 The failure condition, stated explicitly

> The redesign is a **failure** if per-item time falls while **first-answer kappa
> (A9) or gold accuracy (A10) drops** — no matter what the headline kappa does.

Rising inter-rater agreement is not proof the UI improved. The PLOS ONE
crowdworker study found items "erroneously annotated with high agreement and low
uncertainty", and every risk mechanism in §6 pushes agreement up. Validate against
gold, not against each other. And do not engineer disagreement away: Peterson et
al. (CIFAR-10H) showed that training on the full human label distribution improves
out-of-distribution generalisation and adversarial robustness relative to single
hard labels. Annotator disagreement on genuinely ambiguous cows is signal we are
paying for.

### 8.6 Ship order

1. **Before any production day** — §6.1 (`presented`/`answered` events), §6.3
   (`behaviour.not_visible`), §6.4 (escape-rate query), §3.8 (arrow ownership).
   Small; one event kind, one emission point, one `create_class`, one query, one
   capture-phase listener.
2. **Before the second annotator starts** — §6.7 (queue order), §6.2 (first-answer
   kappa), §6.6 (gold items). Unrecoverable retroactively.
3. **Then** the screen itself: §2, §3, §4, §5.
4. **Then** §6.9 (example crops in definitions), §6.8 (padded-fraction logging),
   §4.3 (icon test), the calibration batch and the order-prime measurement.

---

## 9. Sources

Tooling, verified from documentation or source:
[Prodigy web app](https://prodi.gy/docs/web-app) ·
[Prodigy interfaces](https://prodi.gy/docs/api-interfaces) ·
[Prodigy config](https://prodi.gy/docs/install#config) ·
[Prodigy changelog](https://prodi.gy/docs/changelog) ·
[Prodigy computer vision](https://prodi.gy/docs/computer-vision) ·
[Explosion, supervised learning data collection](https://explosion.ai/blog/supervised-learning-data-collection) ·
[CVAT `attribute-switcher.tsx`](https://raw.githubusercontent.com/cvat-ai/cvat/develop/cvat-ui/src/components/annotation-page/attribute-annotation-workspace/attribute-annotation-sidebar/attribute-switcher.tsx) ·
[CVAT `attribute-editor.tsx`](https://raw.githubusercontent.com/cvat-ai/cvat/develop/cvat-ui/src/components/annotation-page/attribute-annotation-workspace/attribute-annotation-sidebar/attribute-editor.tsx) ·
[CVAT AAM basics](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/attribute-annotation-mode-basics/) ·
[CVAT tag mode](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/annotation-with-tags/) ·
[CVAT issue #8400](https://github.com/cvat-ai/cvat/issues/8400) ·
[Label Studio keymap.json](https://raw.githubusercontent.com/HumanSignal/label-studio/develop/web/libs/editor/src/core/settings/keymap.json) ·
[Label Studio `<Choices>`](https://labelstud.io/tags/choices) ·
[Labelbox shortcuts](https://docs.labelbox.com/docs/keyboard-shortcuts) ·
[Roboflow shortcuts](https://docs.roboflow.com/datasets/annotate/annotate/use-roboflow-annotate/keyboard-shortcuts.md) ·
[Encord label editor shortcuts](https://docs.encord.com/platform-documentation/Annotate/annotate-label-editor/annotate-label-editor-settings-shortcuts) ·
[V7 properties](https://docs.v7labs.com/docs/properties) ·
[Supervisely image tagging](https://supervisely.com/blog/mastering-image-tagging/) ·
[doccano](https://doccano.github.io/doccano/) ·
[Anki studying](https://docs.ankiweb.net/studying.html) ·
[Anki deck options](https://docs.ankiweb.net/deck-options.html) ·
[Gmail shortcuts](https://support.google.com/mail/answer/6594) ·
[GitHub shortcuts](https://docs.github.com/en/get-started/accessibility/keyboard-shortcuts) ·
[Zooniverse glossary](https://help.zooniverse.org/glossary/)

Standards and icon sets:
[WCAG 2.2 SC 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html) ·
[WCAG 2.2 SC 2.5.5](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html) ·
[WCAG SC 1.4.1](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html) ·
[NN/g icon usability](https://www.nngroup.com/articles/icon-usability/) ·
[Lucide `sun.svg`](https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/sun.svg) ·
[Lucide `sun-dim.svg`](https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/sun-dim.svg) ·
[Heroicons](https://heroicons.com/) ·
[Phosphor `sun-horizon.svg`](https://raw.githubusercontent.com/phosphor-icons/core/main/assets/regular/sun-horizon.svg) ·
[Material Symbols](https://github.com/google/material-design-icons)

Human factors and annotation quality:
[Thorpe, Fize & Marlot 1996](https://www.nature.com/articles/381520a0) ·
[View-time limits in crowdsourced classification, arXiv 2412.00260](https://arxiv.org/abs/2412.00260) ·
[Labelling instructions in biomedical imaging, arXiv 2207.09899](https://arxiv.org/abs/2207.09899) ·
[Quality Assured, arXiv 2407.17596](https://arxiv.org/html/2407.17596v2) ·
[CIFAR-10H, arXiv 1908.07086](https://arxiv.org/abs/1908.07086) ·
[Crowdworker medical annotation, PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0254764) ·
[Grid vs item-by-item survey presentation, Couper et al.](https://pmc.ncbi.nlm.nih.gov/articles/PMC4172361/) ·
[Hick's law review, Proctor & Schneider 2018](https://web.ics.purdue.edu/~dws/pubs/ProctorSchneider_2018_QJEP.pdf) ·
[Snapshot Serengeti, Swanson et al. 2015](https://pmc.ncbi.nlm.nih.gov/articles/PMC4460915) ·
[McDonald et al. 2015 (PubMed record)](https://pubmed.ncbi.nlm.nih.gov/26210525/)

Honest notes on provenance: the radiology throughput figures and the 25–75 lux
reading-room bracket were obtained from search summaries, not from the primary
paywalled/403 sources. Superhuman's "Auto-Advance increases throughput by 15% or
more" is marketing copy with no stated methodology and is **not** used above. The
"annotation sessions should not exceed 45 minutes" figure and the "about 7
simultaneously searchable colours" figure are folklore with no traceable primary
source and are not used. Scale Rapid publishes no annotator-facing UI
documentation at all; the only transferable idea from it is the calibration
batch. Per-item time targets in §8.1 are labelled as extrapolation from the cited
floors, not as cited results.
