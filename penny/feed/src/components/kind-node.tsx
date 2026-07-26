import { Activity, CalendarDays, FileText, Heart, Pill } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Kind } from "@/lib/data";

/**
 * The entry's kind, as a filled disc with the glyph knocked out in white.
 *
 * Luma puts a plain grey dot on the spine. The dot is the right size and the
 * right silhouette, so this keeps it and fills it — one solid disc in the
 * kind's hue. It signposts the type from the left margin without the ring and
 * the tinted glyph making a small object out of three colours.
 *
 * The same disc labels the composer's kind chips, at `sm`. Reusing it there
 * rather than drawing a second set of icons is the point: whatever you pick
 * when writing an update is exactly what you will later scan for on the spine.
 */
const NODE: Record<Kind, { icon: typeof Activity; fill: string }> = {
  symptom: { icon: Activity, fill: "bg-symptom" },
  medication: { icon: Pill, fill: "bg-penny" },
  appointment: { icon: CalendarDays, fill: "bg-appointment" },
  wellbeing: { icon: Heart, fill: "bg-wellbeing" },
  admin: { icon: FileText, fill: "bg-admin" },
};

const SIZES = {
  sm: { disc: "size-[18px]", glyph: "size-[10px]" },
  md: { disc: "size-[26px]", glyph: "size-[13px]" },
} as const;

export function KindNode({
  kind,
  size = "md",
  /** Adds a ring in the ground colour so the timeline spine passes behind the
      disc rather than appearing to touch it. Only the spine needs it. */
  onSpine = false,
}: {
  kind: Kind;
  size?: keyof typeof SIZES;
  onSpine?: boolean;
}) {
  const { icon: Icon, fill } = NODE[kind];
  const s = SIZES[size];

  return (
    <span
      aria-hidden
      className={cn(
        "grid shrink-0 place-items-center rounded-full",
        s.disc,
        fill,
        onSpine && "ring-4 ring-ground",
      )}
    >
      <Icon
        className={cn(s.glyph, "text-white dark:text-background")}
        strokeWidth={2.1}
      />
    </span>
  );
}
