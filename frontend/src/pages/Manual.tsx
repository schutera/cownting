import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { SectionLabel } from "../components/ui";

/**
 * User manual: plain, click-level how-tos for the two things a non-technical user
 * does — upload a day of footage, and draw count / panel areas. Kept deliberately
 * concrete ("click the gear", "drag a corner") so it reads as steps, not concepts.
 * Linked from the header nav. Static content; no data fetching.
 */
export default function Manual() {
  return (
    <div className="flex flex-col gap-12 animate-fade-slide-in max-w-3xl">
      <header>
        <SectionLabel>MANUAL</SectionLabel>
        <h1 className="font-display text-3xl sm:text-4xl font-light text-near-black leading-tight mt-1">
          How to use Cownting
        </h1>
        <p className="text-gray-mid text-sm mt-2">
          Two things to learn: uploading a day of footage, and drawing the areas that
          turn cow positions into counts. Each is a short click-by-click below.
        </p>
        <nav className="mt-5 flex flex-wrap gap-x-5 gap-y-1.5 text-[13px]">
          <a href="#upload" className="text-accent hover:text-accent-deep">1 · Upload footage</a>
          <a href="#count-areas" className="text-accent hover:text-accent-deep">2 · Count areas</a>
          <a href="#panel-areas" className="text-accent hover:text-accent-deep">3 · Panel areas</a>
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
            In the top navigation, click <B>Data</B>.
          </li>
          <li>
            Under <B>Add a day of footage</B> you’ll see one drop tile per camera
            (four by default). <B>Drag a video onto a tile</B>, or click the tile to
            pick a file. A thumbnail appears when it’s attached.
          </li>
          <li>
            Need more or fewer cameras? Click <B>+ Add camera</B> for another tile, or
            the <B>×</B> on a tile to remove it. Give each tile a short{" "}
            <B>camera name</B> (letters, digits, <Code>_</Code> or <Code>-</Code>) —
            keep names consistent between days so areas can be reused.
          </li>
          <li>
            Click <B>Upload</B>. A progress bar shows the three stages —{" "}
            <em>sampling frames → detecting cows → placing in areas</em>. You can
            leave the page; processing continues on the server.
          </li>
          <li>
            If Cownting can’t read the recording date from a clip, it asks for the{" "}
            <B>capture day</B> — pick it and upload again.
          </li>
          <li>
            When it finishes, the new day becomes your active day on the{" "}
            <B>Dashboard</B>. If a camera came back unusable (a black or empty image),
            the completion note flags it.
          </li>
        </Steps>
        <Callout>
          <B>A camera looks broken?</B> On the <B>Data</B> page, click the{" "}
          <B>⚙ gear</B> on a day card to open its camera manager. Each camera shows a
          health badge (obscured, stopped early, or no cows). You can{" "}
          <B>delete</B> one bad stream and <B>upload a replacement</B> into the same
          day — the other cameras are left untouched.
        </Callout>
      </Section>

      {/* 2 — COUNT AREAS ------------------------------------------------------- */}
      <Section id="count-areas" n="2" title="Draw count areas">
        <p>
          A <B>count area</B> is a named region you draw on a camera’s view. Any cow
          standing inside it is tallied to that area. You draw it twice: once on the{" "}
          <B>camera frame</B> (this is what actually counts), and once on the{" "}
          <B>orthophoto</B> (the map, so the area shows in the right place).
        </p>
        <Steps>
          <li>
            Open the <B>Dashboard</B>. In the <B>Cameras / Segmentation</B> panel on
            the left, find the camera you want.
          </li>
          <li>
            Hover the camera image and click the <B>⚙ gear</B> in its top-right
            corner. This opens the <B>count-area editor</B> for that camera.
          </li>
          <li>
            <B>Pick a clear frame to draw on</B> (top of the editor). It opens on the
            moment you had selected on the dashboard timeline; use{" "}
            <B>‹ Prev / Next ›</B> to step to a frame where the ground is easy to see.
            The frame is only a backdrop — it doesn’t change your areas.
          </li>
          <li>
            Click <B>+ Add area</B>. On the <B>left (camera frame)</B>, click around
            the region to drop corner points; they connect into a filled shape. You
            need at least three.
          </li>
          <li>
            <B>Adjust points:</B> <B>drag any corner</B> to move it. Double-click a
            corner to delete it. <B>Undo point</B> removes the last one; <B>Clear</B>{" "}
            starts that shape over. Scroll to zoom, drag the background to pan.
          </li>
          <li>
            On the <B>right (orthophoto)</B>, draw the matching shape in the same spot
            on the map. This is only for placement on the map.
          </li>
          <li>
            Give the area a <B>name</B> in its chip. Add more areas with{" "}
            <B>+ Add area</B>; switch between them with the chip’s <B>▤</B> button.
          </li>
          <li>
            Click <B>Save count areas</B>. Cownting re-assigns every cow to the areas
            in the background (“the box is working” spinner) and the dashboard counts
            update.
          </li>
        </Steps>
        <Callout>
          <B>Reuse areas from another day or camera.</B> Instead of drawing from
          scratch, click <B>Import areas…</B> in the editor, pick a source{" "}
          <B>day</B> and <B>camera</B>, and import its shapes. Then fine-tune by{" "}
          dragging the corners — handy when a camera moved only a little between days.
        </Callout>
      </Section>

      {/* 3 — PANEL AREAS ------------------------------------------------------- */}
      <Section id="panel-areas" n="3" title="Draw panel (shelter) areas">
        <p>
          A <B>panel area</B> marks the shade under a solar panel. A cow whose feet
          fall inside one counts as <B>sheltering</B> (under a panel). It’s drawn
          exactly like a count area — only the meaning differs.
        </p>
        <Steps>
          <li>
            Open the same editor as above (Dashboard → camera <B>⚙ gear</B>).
          </li>
          <li>
            At the top-right of the editor, switch the toggle from{" "}
            <B>count areas</B> to <B>panel areas</B>. Your count areas are kept — you’re
            now editing a separate set of shapes.
          </li>
          <li>
            Click <B>+ Add panel</B> and draw the shaded footprint of the panel on the{" "}
            <B>camera frame</B>, then the matching shape on the <B>orthophoto</B> —
            same drawing, dragging, and double-click-to-delete as count areas.
          </li>
          <li>
            Click <B>Save panel areas</B>. Sheltering counts on the dashboard update.
          </li>
        </Steps>
        <Callout>
          Count areas and panel areas are independent and saved separately. A cow can
          be counted in a count area <em>and</em> be sheltering under a panel at the
          same time.
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
    <ol className="flex flex-col gap-2.5 list-decimal pl-5 marker:text-gray-tertiary marker:font-mono marker:text-[12px]">
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

function B({ children }: { children: ReactNode }) {
  return <span className="text-near-black font-medium">{children}</span>;
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-[12px] bg-surface-sunk px-1 py-0.5 rounded text-near-black">{children}</code>;
}
