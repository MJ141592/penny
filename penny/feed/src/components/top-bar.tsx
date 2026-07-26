"use client";

import Image from "next/image";
import { useState } from "react";

import {
  ClinicalSummaryPanel,
  ClinicalSummaryTrigger,
} from "@/components/clinical-summary";
import { Collapsible } from "@/components/ui/collapsible";
import { person } from "@/lib/data";

/**
 * The only chrome on the page.
 *
 * It replaces the icon rail: with one screen to look at, a permanent vertical
 * navigation was four-fifths empty and pushed the record off-centre. Identity
 * and search are the two things that genuinely belong at this level.
 */
export function TopBar() {
  return (
    // Sticky and rule-less. The record scrolls for years, so the wordmark and
    // search have to stay reachable — and with the ground tinted and the cards
    // white, the blur alone separates the bar from what passes under it. A
    // border would have drawn a second horizontal line into a layout whose
    // whole job is to have none.
    <header className="sticky top-0 z-40 bg-ground/75 backdrop-blur-md">
      <div className="mx-auto flex h-[72px] max-w-[1400px] items-center gap-3 px-8 xl:px-12">
        <Image src="/penny-mark.svg" alt="" width={30} height={30} priority />
        <span className="text-[19px] leading-none font-semibold tracking-tight">
          Penny
        </span>

        <div className="ml-auto flex items-center gap-5">
          <button
            type="button"
            className="text-[13.5px] text-muted-foreground transition-colors hover:text-foreground"
          >
            Search{" "}
            <kbd className="ml-0.5 font-sans text-[12.5px] tracking-tight">
              ⌘K
            </kbd>
          </button>
          <span
            className="grid size-9 place-items-center rounded-full bg-penny text-[11.5px] font-semibold tracking-wide text-white dark:text-background"
            title="Yuval Alden"
          >
            YA
          </span>
        </div>
      </div>
    </header>
  );
}

/**
 * Who the record is about — name, age, place, and how many people feed it.
 *
 * The clinical summary trigger sits next to the name, on the header itself:
 * generating a summary is an act on the record as a whole, not on any one
 * day, so it belongs with identity rather than floating in the timeline. The
 * panel it opens still runs full-width, directly beneath this row.
 */
export function PersonHeader() {
  const [open, setOpen] = useState(false);

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <div className="flex items-start gap-5 pt-9 pb-6">
        <span className="grid size-[68px] shrink-0 place-items-center rounded-full bg-penny-wash text-[22px] font-semibold text-penny">
          {person.initials.charAt(0)}
        </span>

        <div className="min-w-0 pt-1.5">
          <h1 className="flex flex-wrap items-baseline gap-x-3 text-[30px] leading-none font-semibold tracking-tight">
            {person.name}
            <span className="font-sans text-[14.5px] tracking-normal text-muted-foreground">
              {person.age} · {person.place}
            </span>
            <ClinicalSummaryTrigger open={open} />
          </h1>
          <p className="mt-2.5 text-[14px] text-muted-foreground">
            {person.contributorCount} people contribute to this record
          </p>
        </div>
      </div>

      <ClinicalSummaryPanel />
    </Collapsible>
  );
}
