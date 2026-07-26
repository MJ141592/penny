import { AddUpdate } from "@/components/add-update";
import { TimelineEntry } from "@/components/timeline-entry";
import { entries, type Entry } from "@/lib/data";

/**
 * The care timeline.
 *
 * No filters and no scope tabs: a row of controls above the first entry is the
 * surest way to make a record look like a dashboard. Search in the top bar is
 * the escape hatch now that the history runs to years rather than days.
 */
export function Timeline() {
  const years = groupByYear(entries);

  return (
    <main className="min-w-0 flex-1">
      <div className="mt-10 mb-9">
        <AddUpdate />
      </div>

      {years.map(({ year, items }) => (
        <section key={year}>
          <YearMarker year={year} />
          {items.map((entry) => (
            <TimelineEntry key={entry.id} entry={entry} />
          ))}
        </section>
      ))}
    </main>
  );
}

/**
 * Walks the already-sorted list and starts a new group when the year changes.
 *
 * It does not bucket: a list that came back out of order shows up as a
 * repeated year marker rather than being silently tidied into looking right.
 */
function groupByYear(list: Entry[]): { year: string; items: Entry[] }[] {
  const groups: { year: string; items: Entry[] }[] = [];
  for (const entry of list) {
    const last = groups[groups.length - 1];
    if (last && last.year === entry.year) last.items.push(entry);
    else groups.push({ year: entry.year, items: [entry] });
  }
  return groups;
}

/**
 * The year, printed once where it changes.
 *
 * Kept quiet on purpose: it is orientation, not a headline, and it is what
 * lets every date below it stay short — "Sat 25 Jul", never "Sat 25 Jul 2026".
 */
function YearMarker({ year }: { year: string }) {
  return (
    // Same three columns as an entry, so the year sits in the date gutter and
    // its dot lands dead on the spine that runs through every node below it.
    <div className="relative grid grid-cols-[104px_26px_minmax(0,1fr)] gap-x-5 pt-4 pb-6">
      <span
        aria-hidden
        className="absolute top-[22px] bottom-0 left-[137px] w-px -translate-x-1/2 bg-rule"
      />
      <div className="text-[12px] font-semibold tracking-[0.06em] text-muted-foreground">
        {year}
      </div>
      <span className="relative z-10 mt-1 justify-self-center">
        <span className="block size-[7px] rounded-full bg-rule ring-4 ring-ground" />
      </span>
    </div>
  );
}
