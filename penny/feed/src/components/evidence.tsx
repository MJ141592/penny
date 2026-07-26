"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ChatBubble } from "@/components/chat-bubble";
import { cn } from "@/lib/utils";
import type { Message } from "@/lib/data";

/**
 * The messages an entry was drawn from, folded away by default.
 *
 * The entry states a conclusion; this is where you check it. Keeping it closed
 * means the record reads as a record rather than a chat log, but the evidence
 * is always one click away — and it is the whole reason the family believes
 * the line above it.
 */
export function Evidence({ messages }: { messages: Message[] }) {
  const [open, setOpen] = useState(false);

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border border-rule px-3.5 py-[7px]",
          "text-[12.5px] font-medium text-penny transition-colors hover:bg-band",
        )}
      >
        {open ? "Hide the evidence" : "See the evidence"}
        <ChevronRight
          className={cn("size-3.5 transition-transform", open && "rotate-90")}
        />
      </CollapsibleTrigger>

      <CollapsibleContent>
        <div className="mt-4 flex max-w-[520px] flex-col gap-2">
          {messages.map((m, i) => (
            <ChatBubble key={i} message={m} />
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
