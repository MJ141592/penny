import { PersonAvatar } from "@/components/person-avatar";
import { cn } from "@/lib/utils";
import { conditions, medications, people, vitals } from "@/lib/data";

const CARERS = ["yuval", "matthew", "liz", "owen"] as const;

/**
 * The standing facts, beside the timeline rather than inside it.
 *
 * These do not happen on a day, so they have no place on a spine ordered by
 * date — but they are the context every entry is read against, which is why
 * they sit alongside instead of on a second screen.
 *
 * Identity moved out of here and into the page header: it was the one panel
 * that framed everything else rather than sitting level with it.
 */
function PanelLabel({
  children,
  action,
}: {
  children: React.ReactNode;
  action?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <h3 className="text-[10.5px] font-semibold tracking-[0.1em] text-muted-foreground">
        {children}
      </h3>
      {action && (
        <button className="text-[13px] text-penny hover:underline">
          {action}
        </button>
      )}
    </div>
  );
}

export function ContextColumn() {
  return (
    // Sticky and self-start: these facts stay true for every year the timeline
    // scrolls through, so they stay on screen instead of being left behind at
    // the top of a history that runs for pages. `self-start` is what stops the
    // flex row stretching the column to full height and killing the stick.
    <aside className="sticky top-6 hidden max-h-[calc(100vh-3rem)] w-[330px] shrink-0 flex-col gap-9 self-start overflow-y-auto pt-10 pb-6 lg:flex">
      <section>
        <dl className="space-y-3.5 rounded-lg bg-band px-4 py-4">
          {vitals.map((item) => (
            <div key={item.label}>
              <dt
                className={cn(
                  "text-[10.5px] font-semibold tracking-[0.1em]",
                  item.alert ? "text-symptom" : "text-muted-foreground",
                )}
              >
                {item.label}
              </dt>
              <dd className="mt-1 text-[13.5px] leading-snug">
                {item.value}
                {item.link && (
                  <>
                    {" · "}
                    <button className="font-medium text-penny hover:underline">
                      {item.link}
                    </button>
                  </>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section>
        <PanelLabel>Medications</PanelLabel>
        <div className="mt-4 space-y-4">
          {medications.map((m) => (
            <div
              key={m.name}
              className={cn(
                "border-l-2 pl-3.5",
                m.changed ? "border-symptom" : "border-rule",
              )}
            >
              <div className="flex items-baseline gap-2">
                <span className="text-[14px] font-semibold">{m.name}</span>
                {m.changed && (
                  <span className="ml-auto text-[9.5px] font-bold tracking-[0.09em] text-symptom">
                    Changed
                  </span>
                )}
              </div>
              <div className="mt-0.5 text-[12.5px] text-muted-foreground">
                {m.detail}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <PanelLabel>Living with</PanelLabel>
        <div className="mt-4 space-y-3.5">
          {conditions.map((c) => (
            <div key={c.name}>
              <div className="text-[14px] font-medium">{c.name}</div>
              <div className="mt-0.5 text-[12.5px] text-muted-foreground">
                {c.detail}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <PanelLabel>Cared by</PanelLabel>
        <div className="mt-4 space-y-3">
          {CARERS.map((id) => (
            <div key={id} className="flex items-center gap-2.5">
              <PersonAvatar id={id} size="sm" />
              <span className="text-[13.5px] font-medium">{people[id].name}</span>
            </div>
          ))}
        </div>
      </section>
    </aside>
  );
}
