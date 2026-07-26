import { ContextColumn } from "@/components/context-column";
import { Timeline } from "@/components/timeline";
import { PersonHeader, TopBar } from "@/components/top-bar";

export default function Page() {
  return (
    <div className="min-h-screen">
      <TopBar />

      {/* One measure for the whole document: the name and the timeline both
          start on the same left edge, which is what makes the page read as a
          record rather than as stacked widgets. */}
      <div className="mx-auto w-full max-w-[1400px] px-8 pb-28 xl:px-12">
        <PersonHeader />

        <div className="flex min-w-0 gap-14">
          <Timeline />
          <ContextColumn />
        </div>
      </div>
    </div>
  );
}
