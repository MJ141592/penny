"use client";

import {
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { clinicalSummary, entries } from "@/lib/data";

/**
 * The control that asks for a summary — split from the panel below so it can
 * sit next to the name, while the panel it opens stays full-width under the
 * header. Both are descendants of the same Collapsible root in PersonHeader,
 * which is all Radix requires — trigger and content don't need to be adjacent.
 */
export function ClinicalSummaryTrigger({ open }: { open: boolean }) {
  return (
    <CollapsibleTrigger className="text-[13.5px] text-penny hover:underline">
      {open ? "Hide clinical summary ↑" : "Generate clinical summary ↧"}
    </CollapsibleTrigger>
  );
}

/**
 * What Penny hands the GP.
 *
 * Closed until somebody asks. A summary is a reading of the record at a
 * moment, not part of it — leaving one open above the timeline would put a
 * stale interpretation between the family and the thing it interpreted.
 *
 * A tinted block, not a card and not a timeline entry: the point is that this
 * is a different kind of object from the record beneath it — something you
 * generate, print, hand over, and throw away.
 */
export function ClinicalSummaryPanel() {
  const s = clinicalSummary;

  return (
    <CollapsibleContent>
      <div className="mb-9 rounded-lg bg-band px-7 py-6">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h3 className="text-[19px] leading-none font-semibold tracking-tight">
            {s.title}
          </h3>
          <p className="text-[12.5px] text-muted-foreground">
            Generated {s.generatedAt} · {s.period} · {s.entryCount} entries
          </p>

          <div className="ml-auto flex items-center gap-4">
            <button
              type="button"
              className="text-[13px] text-penny hover:underline"
            >
              Copy
            </button>
            <button
              type="button"
              className="text-[13px] text-penny hover:underline"
            >
              Download PDF
            </button>
          </div>
        </div>

        <p className="mt-5 max-w-[76ch] text-[15.5px] leading-relaxed text-balance">
          {s.lead}
        </p>

        <div className="mt-7 space-y-6">
          {s.sections.map((section) => (
            <section key={section.heading}>
              <h4 className="text-[10.5px] font-semibold tracking-[0.1em] text-muted-foreground">
                {section.heading}
              </h4>
              <p className="mt-1.5 max-w-[76ch] text-[14.5px] leading-relaxed">
                {section.body}
              </p>
              <Citations ids={section.cites} />
            </section>
          ))}
        </div>

        {/* Three lists, side by side: what to ask, what to watch, and what
            the record cannot tell you. The last one is the honest half — a
            summary that only reports what it found reads as more complete
            than it is. */}
        <div className="mt-8 grid gap-7 border-t border-rule pt-6 lg:grid-cols-3">
          <List title="Questions for the doctor" items={s.questions} />
          <List title="Watch for" items={s.watch} tone="symptom" />
          <List title="Not in the record" items={s.gaps} muted />
        </div>

        <p className="mt-7 border-t border-rule pt-4 text-[11.5px] leading-snug text-muted-foreground">
          Written by Penny from the family&rsquo;s own messages. Every line
          above traces to an entry you can open. Penny reports what was said
          and when — it does not diagnose, and it does not decide what is
          connected to what.
        </p>
      </div>
    </CollapsibleContent>
  );
}

/**
 * The entries a section was drawn from.
 *
 * This is the same promise the timeline makes with "See the evidence", one
 * level up: a claim in a clinical summary is only as good as the ability to
 * jump to the message behind it.
 */
function Citations({ ids }: { ids: string[] }) {
  const cited = ids
    .map((id) => entries.find((e) => e.id === id))
    .filter((e) => e !== undefined);

  if (cited.length === 0) return null;

  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {cited.map((entry) => (
        <button
          key={entry.id}
          type="button"
          className={cn(
            "rounded-full border border-rule bg-ground px-2.5 py-1",
            "text-[11.5px] text-muted-foreground transition-colors",
            "hover:border-penny/40 hover:text-penny",
          )}
        >
          {entry.date} · {entry.headline}
        </button>
      ))}
    </div>
  );
}

function List({
  title,
  items,
  tone,
  muted,
}: {
  title: string;
  items: string[];
  tone?: "symptom";
  muted?: boolean;
}) {
  return (
    <div>
      <h4
        className={cn(
          "text-[10.5px] font-semibold tracking-[0.1em]",
          tone === "symptom" ? "text-symptom" : "text-muted-foreground",
        )}
      >
        {title}
      </h4>
      <ul className="mt-2.5 space-y-2">
        {items.map((item) => (
          <li
            key={item}
            className={cn(
              "flex gap-2 text-[13.5px] leading-snug",
              muted && "text-muted-foreground",
            )}
          >
            <span aria-hidden className="text-muted-foreground">
              ·
            </span>
            <span className="min-w-0">{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
