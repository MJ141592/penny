import Image from "next/image";

import { Evidence } from "@/components/evidence";
import { KindNode } from "@/components/kind-node";
import { cn } from "@/lib/utils";
import type { Entry } from "@/lib/data";

const WEEKDAY: Record<string, string> = {
  Mon: "Monday",
  Tue: "Tuesday",
  Wed: "Wednesday",
  Thu: "Thursday",
  Fri: "Friday",
  Sat: "Saturday",
  Sun: "Sunday",
};

/**
 * Splits "Sat 25 Jul" into the two lines the gutter prints.
 *
 * Done here rather than in the data so the 27 entries keep one short date
 * string each — the presentation decided how to break it, and the record
 * should not have to be rewritten when that changes again.
 */
function splitDate(date: string): { day: string; weekday: string } {
  const [abbrev, ...rest] = date.split(" ");
  const full = abbrev && WEEKDAY[abbrev];
  if (!full) return { day: date, weekday: "" };
  // Wednesday and Thursday are the two longest weekday names — spelled out
  // under a short date, they wrap in the narrow gutter and stop it looking
  // quiet. Dropping just those two keeps the rest legible without the clutter.
  const weekday = abbrev === "Wed" || abbrev === "Thu" ? "" : full;
  return { day: rest.join(" "), weekday };
}

/**
 * One entry in the record: a date in the margin, a node on the spine, and a
 * card holding what happened.
 *
 * The card is back because the entries now run to years rather than a week —
 * at that length continuous text stops reading as a list and starts reading as
 * a wall. A white card on tinted ground gives each entry an edge without a
 * rule having to draw one.
 */
export function TimelineEntry({ entry }: { entry: Entry }) {
  const { day, weekday } = splitDate(entry.date);

  return (
    <article className="relative grid grid-cols-[104px_26px_minmax(0,1fr)] gap-x-5 pb-7">
      {/* The spine runs the full height of every entry, so consecutive nodes
          join into one unbroken line down the page. */}
      <span
        aria-hidden
        className="absolute inset-y-0 left-[137px] w-px -translate-x-1/2 bg-rule"
      />

      {/* Left-aligned, not right: it puts the date's first character on the
          same vertical as the avatar, the composer and every year marker, so
          one straight line runs down the left edge of the whole page. */}
      <div className="pt-3.5">
        <div className="text-[14px] leading-none font-semibold tracking-tight">
          {day}
        </div>
        {weekday && (
          <div className="mt-1.5 text-[13px] leading-none text-muted-foreground">
            {weekday}
          </div>
        )}
      </div>

      <span className="relative z-10 mt-3">
        <KindNode kind={entry.kind} onSpine />
      </span>

      {/* The border is transparent until hover rather than absent, so the card
          does not shift by a pixel when the outline arrives. At rest the white
          against the warm ground is edge enough. */}
      <div
        className={cn(
          "min-w-0 rounded-xl border border-transparent bg-card p-5",
          "shadow-[0_1px_2px_rgb(0_0_0/0.03)] transition-colors hover:border-rule",
        )}
      >
        <div className="flex gap-5">
          <div className="min-w-0 flex-1">
            <span className="text-[13px] text-muted-foreground tabular-nums">
              {entry.at}
            </span>

            <h3 className="mt-1.5 text-[17.5px] leading-snug font-semibold tracking-tight text-balance">
              {entry.headline}
            </h3>

            <p className="mt-1.5 max-w-[68ch] text-[14px] leading-relaxed text-muted-foreground">
              {entry.summary}
            </p>

            <p className="mt-2.5 text-[12.5px] text-muted-foreground">
              {entry.provenance}
            </p>

            <div className="mt-3.5">
              <Evidence messages={entry.messages} />
            </div>
          </div>

          {/* Inside the card, and square: the photograph is filed with the
              entry, not published under it. */}
          {entry.image && (
            <div className="relative hidden size-[168px] shrink-0 overflow-hidden rounded-lg bg-band sm:block">
              <Image
                src={entry.image.src}
                alt={entry.image.alt}
                fill
                sizes="168px"
                className="object-cover"
              />
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
