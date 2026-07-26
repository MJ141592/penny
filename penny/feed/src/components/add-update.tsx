"use client";

import { useState } from "react";
import { ArrowUp, ImagePlus } from "lucide-react";

import { KindNode } from "@/components/kind-node";
import { cn } from "@/lib/utils";
import { kindStyle, type Kind } from "@/lib/data";

const KINDS: Kind[] = [
  "symptom",
  "medication",
  "appointment",
  "wellbeing",
  "admin",
];

/**
 * The composer at the head of the record.
 *
 * Collapsed it is one white line — the same white as the cards below it, so it
 * reads as the first thing in the list rather than as chrome above it.
 *
 * Open, it *overlays* rather than expands. Growing the composer would push the
 * whole timeline down the moment somebody clicked into it, so the entry you
 * were looking at when you decided to add something moves out from under you.
 * The collapsed bar stays in the flow holding its space; the panel floats over
 * the record on a raised layer.
 *
 * The fields mirror the shape of an entry — kind, headline, detail — because
 * that is what Penny would otherwise have to infer. Asking for the three parts
 * directly is cheaper than extracting them from a paragraph and being wrong.
 */
export function AddUpdate() {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("symptom");
  const [headline, setHeadline] = useState("");
  const [detail, setDetail] = useState("");

  const empty = headline.trim() === "" && detail.trim() === "";

  function close() {
    setOpen(false);
    setHeadline("");
    setDetail("");
    setKind("symptom");
  }

  return (
    // Anchors the floating panel, and keeps the collapsed bar's height in the
    // layout so nothing below it moves when the panel appears.
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "w-full rounded-xl border border-transparent bg-card px-5 py-4 text-left",
          "text-[14px] text-muted-foreground shadow-[0_1px_2px_rgb(0_0_0/0.03)]",
          "transition-colors hover:border-rule",
        )}
      >
        Add an update
      </button>

      {open && (
        <>
          {/* Click anywhere off the panel to put it away, but only while
              nothing has been typed — a stray click should not cost a note. */}
          <div
            aria-hidden
            className="fixed inset-0 z-40"
            onClick={() => {
              if (empty) close();
            }}
          />

          <div
            className={cn(
              "absolute inset-x-0 top-0 z-50 rounded-xl border border-rule bg-card",
              "shadow-[0_12px_32px_-8px_rgb(0_0_0/0.14),0_2px_6px_rgb(0_0_0/0.05)]",
            )}
            onKeyDown={(e) => {
              if (e.key === "Escape" && empty) close();
            }}
          >
            <div className="flex flex-wrap gap-1.5 px-5 pt-4">
              {KINDS.map((k) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setKind(k)}
                  aria-pressed={kind === k}
                  className={cn(
                    "flex items-center gap-1.5 rounded-full py-1.5 pr-3 pl-1.5",
                    "text-[12px] font-medium transition-colors",
                    kind === k
                      ? `${kindStyle[k].bg} ${kindStyle[k].text}`
                      : "text-muted-foreground hover:bg-band",
                  )}
                >
                  {/* The same disc that will mark this entry on the spine, so
                      the choice is made against the thing it produces. */}
                  <KindNode kind={k} size="sm" />
                  {kindStyle[k].label}
                </button>
              ))}
            </div>

            <input
              autoFocus
              value={headline}
              onChange={(e) => setHeadline(e.target.value)}
              placeholder="What happened?"
              className={cn(
                "mt-3 w-full bg-transparent px-5 text-[17.5px] font-semibold tracking-tight",
                "outline-none placeholder:font-normal placeholder:text-muted-foreground",
              )}
            />

            <textarea
              rows={4}
              value={detail}
              onChange={(e) => setDetail(e.target.value)}
              placeholder="What you saw, what was said, and anything that happens next."
              className={cn(
                "mt-2.5 w-full resize-none bg-transparent px-5 text-[14px] leading-relaxed",
                "outline-none placeholder:text-muted-foreground",
              )}
            />

            <div className="flex items-center gap-3 border-t border-rule px-5 py-3">
              <button
                type="button"
                aria-label="Attach a photo"
                className="grid size-8 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-band hover:text-foreground"
              >
                <ImagePlus className="size-[17px]" strokeWidth={1.9} />
              </button>
              <span className="text-[12px] text-muted-foreground">
                Filed to today · Sat 25 Jul
              </span>

              <button
                type="button"
                disabled={headline.trim() === ""}
                onClick={close}
                aria-label="Add to the record"
                className={cn(
                  "ml-auto grid size-9 shrink-0 place-items-center rounded-full",
                  "bg-penny text-white transition-opacity dark:text-background",
                  "disabled:opacity-30",
                )}
              >
                <ArrowUp className="size-[17px]" strokeWidth={2.2} />
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
