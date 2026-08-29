import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Button, CogIcon, SectionLabel } from "../components/ui";
import { ModeToggle } from "./CountArea";

/**
 * User manual: plain, click-level how-tos for the things a non-technical user
 * does — upload a day of footage, draw count / panel areas, and label animals
 * (answer the questions, fix a wrong outline). Kept deliberately concrete
 * ("click the gear", "drag a corner").
 *
 * Code-as-documentation: every control shown inline is the REAL component from the
 * app (the shared CogIcon, the real <Button>, the real <ModeToggle>), not a
 * screenshot or a hand-copied replica — so when a button changes, the manual
 * changes with it and can't drift out of date. Static, no data fetching.
 */
const noop = () => {};

export default function Manual() {
  return (
    <div className="flex flex-col gap-12 animate-fade-slide-in max-w-3xl">
      <header>
        <SectionLabel>MANUAL</SectionLabel>
        <h1 className="font-display text-3xl sm:text-4xl font-light text-near-black leading-tight mt-1">
          How to use Cownting
        </h1>
        <p className="text-gray-mid text-sm mt-2">
          Three things to learn: uploading a day of footage, drawing the areas that
          turn cow positions into counts, and labeling animals one by one. Each is a
          short click-by-click below — the buttons shown inline are the real ones
          you’ll click.
        </p>
        <nav className="mt-5 flex flex-wrap gap-x-5 gap-y-1.5 text-[13px]">
          <a href="#upload" className="text-accent hover:text-accent-deep">1 · Upload footage</a>
          <a href="#manage-cameras" className="text-accent hover:text-accent-deep">2 · Manage cameras</a>
          <a href="#count-areas" className="text-accent hover:text-accent-deep">3 · Count areas</a>
          <a href="#panel-areas" className="text-accent hover:text-accent-deep">4 · Panel areas</a>
          <a href="#label-classify" className="text-accent hover:text-accent-deep">5 · Label animals</a>
          <a href="#label-outline" className="text-accent hover:text-accent-deep">6 · Fix an outline</a>
        </nav>
      </header>

      {/* 1 — UPLOAD ------------------------------------------------------------ */}
      <Section id="upload" n="1" title="Upload a day of footage">
        <p>
          A “day” is one recording session across all your cameras — one video file
          per camera. Cownting samples frames, detects the cows, and places them
          automatically.
        </p>
        <Steps>
          <li>
            In the top navigation, click <NavPill>Data</NavPill>.
          </li>
          <li>
            Under <B>Add a day of footage</B> you’ll see one drop tile per camera
            (four by default). <B>Drag a video onto a tile</B>, or click the tile to
            pick a file. A thumbnail appears when it’s attached.
          </li>
          <li>
            Need more or fewer cameras? Click{" "}
            <InlineBtn><Button variant="ghost" onClick={noop}>+ Add camera</Button></InlineBtn>{" "}
            for another tile, or the <B>×</B> on a tile to remove it. Give each tile a
            short <B>camera name</B> (letters, digits, <Code>_</Code> or{" "}
            <Code>-</Code>) — keep names consistent between days so areas can be reused.
          </li>
          <li>
            Click{" "}
            <InlineBtn><Button onClick={noop}>Upload 4 cameras</Button></InlineBtn>. A
            progress bar shows the three stages —{" "}
            <em>sampling frames → detecting cows → placing in areas</em>. You can leave
            the page; processing continues on the server.
          </li>
          <li>
            If Cownting can’t read the recording date from a clip, it asks for the{" "}
            <B>capture day</B> — pick it and upload again.
          </li>
          <li>
            When it finishes, the new day becomes your active day on the{" "}
            <NavPill>Dashboard</NavPill>. If a camera came back unusable (a black or
            empty image), the completion note flags it.
          </li>
        </Steps>
        <Callout>
          <B>A camera looks broken, or the cameras don’t line up?</B> If one came back
          unusable (a black or empty image), drop it and re-upload — see{" "}
          <a href="#manage-cameras" className="text-accent hover:text-accent-deep">
            Manage cameras
          </a>{" "}
          next. The bar under each tile in the dashboard’s <B>Cameras</B> card shows
          when that camera has frames, and the card warns when the cameras cover very
          different windows (counts only line up where the cameras overlap).
        </Callout>
      </Section>

      {/* 2 — MANAGE CAMERAS ---------------------------------------------------- */}
      <Section id="manage-cameras" n="2" title="Manage cameras">
        <PowerNote />
        <p>
          Each day’s cameras can be checked and fixed one at a time. If a camera came
          back unusable — an obscured lens (black frames), a clip that stopped early,
          or a view with no cows — drop that one stream and upload a healthy
          replacement, without touching the other cameras or re-doing the whole day.
        </p>
        <Steps>
          <li>
            Open the camera manager: on the <NavPill>Dashboard</NavPill>, click the{" "}
            <Inline><CogIcon className="w-4 h-4 text-gray-tertiary" /></Inline> gear next
            to <B>CAMERAS</B> in the segmentation panel — or on the{" "}
            <NavPill>Data</NavPill> page, click the <CardGear /> gear on a day card.
          </li>
          <li>
            Each camera is listed with a health badge:{" "}
            <IssueChip>Obscured</IssueChip> (too dark),{" "}
            <IssueChip>Stopped early</IssueChip>, or <IssueChip>No cows</IssueChip> —
            or a calm <B>Healthy</B> when it’s fine.
          </li>
          <li>
            <B>Delete a bad stream:</B> click <DeleteChip /> on that camera, then{" "}
            <B>Confirm delete</B>. Its frames, detections, and areas are removed and
            every other camera is left untouched. This can’t be undone.
          </li>
          <li>
            <B>Clip a stream to sync:</B> if a camera recorded a longer window than the
            others (the coverage bars in the dashboard’s <B>Cameras</B> card show this),
            click{" "}
            <ClipChip /> on it, set the <B>keep</B> window — it defaults to the times
            the other cameras share — and <B>Confirm clip</B>. Frames outside the window
            are set aside so the camera lines up with the rest. Changed your mind?{" "}
            <B>↩ Undo clip</B> on that camera brings its full range back.
          </li>
          <li>
            <B>Add or replace:</B> under <B>Add or replace a camera</B>, drop a clip
            and give it a camera name — an <B>existing name replaces</B> that stream,
            a <B>new name adds</B> one. Click{" "}
            <InlineBtn><Button onClick={noop}>Upload camera</Button></InlineBtn> and
            watch the progress; the day updates when it finishes.
          </li>
        </Steps>
        <Callout>
          Managing cameras — the health view, delete, and replace — is available to{" "}
          <B>powerusers and admins</B> only. If you don’t see the camera-manager gear,
          your account can view the dashboard but not change data — ask an admin.
        </Callout>
      </Section>

      {/* 3 — COUNT AREAS ------------------------------------------------------- */}
      <Section id="count-areas" n="3" title="Draw count areas">
        <p>
          A <B>count area</B> is a named region you draw on a camera’s view. Any cow
          standing inside it is tallied to that area. You draw it twice: once on the{" "}
          <B>camera frame</B> (this is what actually counts), and once on the{" "}
          <B>orthophoto</B> (the map, so the area shows in the right place).
        </p>
        <Steps>
          <li>
            Open the <NavPill>Dashboard</NavPill>. In the <B>Cameras / Segmentation</B>{" "}
            panel on the left, find the camera you want.
          </li>
          <li>
            Hover the camera image and click the <ImageGear /> gear in its top-right
            corner. This opens the <B>count-area editor</B> for that camera.
          </li>
          <li>
            <B>Pick a clear frame to draw on</B> with the <B>Frame</B> slider{" "}
            <SliderPreview /> above the camera frame (left). It opens on the moment you
            had selected on the dashboard timeline; slide to a frame where the ground is
            easy to see. The frame is only a backdrop — it doesn’t change your areas.
          </li>
          <li>
            Click{" "}
            <InlineBtn><Button variant="ghost" onClick={noop}>+ Add area</Button></InlineBtn>.
            On the <B>left (camera frame)</B>, click around the region to drop corner
            points; they connect into a filled shape. You need at least three.
          </li>
          <li>
            <B>Adjust points:</B> <B>drag any corner</B> to move it,{" "}
            <B>double-click</B> a corner to delete it. <B>Undo point</B> removes the
            last one; <B>Clear</B> starts that shape over. Scroll to zoom, drag the
            background to pan.
          </li>
          <li>
            On the <B>right (orthophoto)</B>, draw the matching shape in the same spot
            on the map. This is only for placement on the map.
          </li>
          <li>
            Give the area a <B>name</B> in its chip (the <Glyph>▤</Glyph> button edits
            a chip; <Glyph>✕</Glyph> deletes it). Add more areas with <B>+ Add area</B>;
            switch between them with the chip’s <Glyph>▤</Glyph> button.
          </li>
          <li>
            Click{" "}
            <InlineBtn><Button onClick={noop}>Save count areas</Button></InlineBtn>.
            Cownting re-assigns every cow to the areas in the background (“the box is
            working” spinner) and the dashboard counts update.
          </li>
        </Steps>
        <Callout>
          <B>Reuse areas from another day or camera.</B> Instead of drawing from
          scratch, click{" "}
          <InlineBtn><Button variant="ghost" onClick={noop}>Import areas…</Button></InlineBtn>,
          pick a source <B>day</B>, then step through its <B>cameras</B> with the{" "}
          <B>‹ ›</B> arrows. Each camera’s areas appear on the canvases as a{" "}
          <B>dashed blue preview</B> as you step — so you can see which one fits without
          importing and undoing. Click <B>Import</B> to add them, then drag the corners
          to fine-tune. Handy when a camera moved only a little between days.
        </Callout>
      </Section>

      {/* 4 — PANEL AREAS ------------------------------------------------------- */}
      <Section id="panel-areas" n="4" title="Draw panel (shelter) areas">
        <p>
          A <B>panel area</B> marks the shade under a solar panel. A cow whose feet
          fall inside one counts as <B>sheltering</B> (under a panel). It’s drawn
          exactly like a count area — only the meaning differs.
        </p>
        <Steps>
          <li>
            Open the same editor as above (Dashboard → camera <ImageGear /> gear).
          </li>
          <li>
            At the top-right of the editor, use the toggle{" "}
            <Inline><ModeToggle mode="panel" onMode={noop} /></Inline> to switch from{" "}
            <B>count areas</B> to <B>panel areas</B>. Your count areas are kept —
            you’re now editing a separate set of shapes.
          </li>
          <li>
            Click{" "}
            <InlineBtn><Button variant="ghost" onClick={noop}>+ Add panel</Button></InlineBtn>{" "}
            and draw the shaded footprint of the panel on the <B>camera frame</B>, then
            the matching shape on the <B>orthophoto</B> — same drawing, dragging, and
            double-click-to-delete as count areas.
          </li>
          <li>
            Click{" "}
            <InlineBtn><Button onClick={noop}>Save panel areas</Button></InlineBtn>.
            Sheltering counts on the dashboard update.
          </li>
        </Steps>
        <Callout>
          Count areas and panel areas are independent and saved separately — the
          editor’s <Inline><ModeToggle mode="count" onMode={noop} /></Inline> toggle
          switches between them. A cow can be counted in a count area <em>and</em> be
          sheltering under a panel at the same time.
        </Callout>
      </Section>

      {/* 5 — LABEL: CLASSIFICATION -------------------------------------------- */}
      <Section id="label-classify" n="5" title="Label animals (classification)">
        <p>
          You’re shown one animal at a time — a photo crop with a <B>white ring</B>{" "}
          around the one to judge. Each animal takes <B>three steps</B>: first check
          the outline, then answer the two questions. Anyone signed in can label.
        </p>
        <Steps>
          <li>
            Click <NavPill>Label</NavPill> in the top navigation. The next animal
            appears by itself, at step <B>1 · Outline</B>.
          </li>
          <li>
            <B>Step 1 — check the outline.</B> Press <Kbd>Enter</Kbd> if it’s right,{" "}
            <Kbd>E</Kbd> to fix it (section 6), or <Kbd>X</Kbd> if it isn’t a cow at
            all. You can’t answer the questions until you’ve done this — an answer
            about the wrong animal is worse than no answer.
          </li>
          <li>
            <B>Steps 2 and 3 — answer</B> by clicking a tile or pressing its letter
            (<Kbd>Y</Kbd> <Kbd>X</Kbd> <Kbd>C</Kbd> <Kbd>V</Kbd> <Kbd>B</Kbd>{" "}
            <Kbd>N</Kbd>). The last answer <B>saves automatically</B> and brings the
            next animal.
          </li>
          <li>
            <B>Correct</B>: the same letter again clears an answer; <Kbd>←</Kbd>{" "}
            revisits earlier animals, <Kbd>→</Kbd> returns.
          </li>
          <li>
            <B>Look closer</B>: hold <Kbd>Space</Kbd> to see the whole camera frame
            with this animal ringed and outlined — so you can tell where it is in the
            scene — and hold <Kbd>H</Kbd> to strip the marks off.
          </li>
          <li>
            <B>Can’t judge it?</B> Press <Kbd>F</Kbd>, pick a reason, type one line
            why, and <Kbd>Enter</Kbd>.
          </li>
        </Steps>
      </Section>

      {/* 6 — LABEL: SEGMENTATION ---------------------------------------------- */}
      <Section id="label-outline" n="6" title="Fix a cow’s outline (segmentation)">
        <p>
          At <B>step 1</B> of every animal <OutlineToggle /> you either accept the
          outline or fix it here — when it clips a leg, swallows a neighbour, or
          hugs a shadow.
        </p>
        <Steps>
          <li>
            Press <Kbd>E</Kbd> (or click <B>Fix it</B>). The outline appears with
            draggable corner points.
          </li>
          <li>
            <B>Drag</B> a point to move it, <B>click an edge</B> to add one,{" "}
            <B>double-click</B> a point to delete it — same gestures as the
            count-area editor. <Kbd>R</Kbd> reverts to Cownting’s outline.
          </li>
          <li>
            Not a cow at all? <Kbd>X</Kbd> (<B>Not a cow</B>) marks it a false
            detection instead of sculpting a shape around nothing. It leaves the
            queue for everyone and you move to the next animal.
          </li>
          <li>
            <B>Save outline</B> (<Kbd>Enter</Kbd>) stores your correction and hands
            you the questions for the same animal — with the <B>box redrawn to fit
            the shape you just traced</B>, so you answer about the animal as you
            corrected it. <Kbd>Esc</Kbd> goes back without saving.
          </li>
        </Steps>
        <Callout>
          <B>Scroll to zoom</B> while editing — in for a close look at an edge, out
          for room around the animal when the outline needs to reach further than
          the crop shows. On footage processed before outlines were stored, step 1
          shows the <B>detection box</B> as four draggable corners instead; that
          clears day by day as the outlines are filled in.
        </Callout>
      </Section>

      <footer className="border-t border-border pt-6">
        <p className="text-[13px] text-gray-mid">
          Still stuck? Use <B>Report bug</B> in the header to send us what happened
          (a screenshot helps a lot), or head back to the{" "}
          <Link to="/" className="text-accent hover:text-accent-deep">Dashboard</Link>.
        </p>
      </footer>
    </div>
  );
}

/* ------------------------------------------------------------------ layout bits */
function Section({ id, n, title, children }: { id: string; n: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-28">
      <div className="flex items-baseline gap-3">
        <span className="font-display text-2xl text-accent tabular-nums">{n}</span>
        <h2 className="font-display text-2xl font-light text-near-black leading-tight">{title}</h2>
      </div>
      <div className="mt-4 flex flex-col gap-4 text-sm text-text leading-relaxed">{children}</div>
    </section>
  );
}

function Steps({ children }: { children: ReactNode }) {
  return (
    <ol className="flex flex-col gap-3 list-decimal pl-5 marker:text-gray-tertiary marker:font-mono marker:text-[12px]">
      {children}
    </ol>
  );
}

function Callout({ children }: { children: ReactNode }) {
  return (
    <div className="bg-accent-soft/50 border border-accent/20 rounded-xl px-4 py-3 text-[13px] text-text leading-relaxed">
      {children}
    </div>
  );
}

/* Vertical-align a real control so it reads inline with the prose. `InlineBtn`
   shrinks the (chunky) full-size buttons a touch so they don't blow up line height. */
function Inline({ children }: { children: ReactNode }) {
  return <span className="inline-flex items-center align-middle mx-0.5">{children}</span>;
}
function InlineBtn({ children }: { children: ReactNode }) {
  return <span className="inline-flex items-center align-middle mx-0.5 scale-90 origin-left">{children}</span>;
}

function B({ children }: { children: ReactNode }) {
  return <span className="text-near-black font-medium">{children}</span>;
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-[12px] bg-surface-sunk px-1 py-0.5 rounded text-near-black">{children}</code>;
}

function Glyph({ children }: { children: ReactNode }) {
  return <span className="font-mono text-near-black">{children}</span>;
}

/* A keycap, as the Label page prints its bindings. */
function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-block rounded border border-border bg-surface-sunk px-1 font-mono text-[11px] text-near-black align-middle">
      {children}
    </kbd>
  );
}

/* The Classify | Outline toggle above the Label crop (replica, like the chips). */
function OutlineToggle() {
  return (
    <span className="inline-flex items-center rounded-full border border-border text-[11px] font-mono align-middle mx-0.5 overflow-hidden">
      <span className="px-2 py-0.5 bg-accent-soft text-accent-deep">1 · Outline</span>
      <span className="px-2 py-0.5 text-gray-tertiary">2 · Classify</span>
    </span>
  );
}

/* A nav item as it looks in the header (mono, tracked, uppercased). */
function NavPill({ children }: { children: ReactNode }) {
  return (
    <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-accent align-middle">
      {children}
    </span>
  );
}

/* The gear over a camera image (dark rounded square) — uses the SHARED CogIcon, so
   it always matches the real one on the dashboard. */
function ImageGear() {
  return (
    <span className="inline-grid place-items-center w-7 h-7 rounded-lg bg-black/55 text-white align-middle mx-0.5">
      <CogIcon className="w-4 h-4" />
    </span>
  );
}

/* The gear on a day card — the app renders the ⚙ glyph in a round button. */
function CardGear() {
  return (
    <span className="inline-grid place-items-center w-7 h-7 rounded-full border border-border text-gray-mid text-[13px] align-middle mx-0.5">
      ⚙
    </span>
  );
}

/* The frame picker slider (a real, non-interactive range so it matches the app). */
function SliderPreview() {
  return (
    <span className="inline-flex items-center gap-1.5 align-middle mx-0.5">
      <input
        type="range"
        readOnly
        value={45}
        min={0}
        max={100}
        aria-hidden="true"
        tabIndex={-1}
        className="w-20 accent-accent pointer-events-none align-middle"
      />
      <span className="font-mono text-[11px] text-gray-mid">12:00</span>
    </span>
  );
}

/* A health issue badge, as shown in the camera manager. */
function IssueChip({ children }: { children: ReactNode }) {
  return (
    <span className="text-[12px] px-2.5 py-1 rounded-full bg-warn/10 border border-warn/40 text-warn align-middle mx-0.5">
      {children}
    </span>
  );
}

/* "Powerusers & admins" note — a section is gated to data-managers. */
function PowerNote() {
  return (
    <span className="inline-flex items-center gap-1.5 w-fit text-[11px] px-2.5 py-1 rounded-full bg-accent-soft text-accent-deep border border-accent/20">
      🔒 Powerusers &amp; admins
    </span>
  );
}

/* The delete-camera control as it appears in the camera manager. */
function DeleteChip() {
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-warn border border-warn/40 rounded-full px-3 py-1 align-middle mx-0.5">
      🗑 Delete this camera
    </span>
  );
}

/* The clip-stream control as it appears in the camera manager. */
function ClipChip() {
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-text border border-border rounded-full px-3 py-1 align-middle mx-0.5">
      ✂ Clip this stream
    </span>
  );
}
