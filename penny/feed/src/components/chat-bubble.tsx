import { FileText, ImageIcon, Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PersonAvatar } from "@/components/person-avatar";
import { cn } from "@/lib/utils";
import { people, type Message } from "@/lib/data";

/** Static bars — a waveform reads as a voice note without needing real audio. */
const WAVE = [4, 9, 14, 7, 16, 11, 6, 13, 15, 8, 5, 10, 14, 7, 10, 12, 4, 8];

export function ChatBubble({ message }: { message: Message }) {
  const person = people[message.from];
  const outbound = Boolean(message.outbound);

  return (
    <div
      className={cn(
        "flex items-start gap-1.5",
        outbound && "flex-row-reverse",
      )}
    >
      {/* Level with the sender name rather than the bubble's foot, so a long
          message doesn't strand the avatar at the bottom. */}
      <PersonAvatar id={message.from} size="xs" className="mt-1.5" />

      <div
        className={cn(
          "min-w-0 rounded-xl px-2.5 py-1.5",
          outbound ? "rounded-br-sm bg-penny-wash" : "rounded-bl-sm",
          !outbound && person.wash,
        )}
      >
        <div className={cn("text-[10px] font-bold tracking-wide", person.tone)}>
          {person.name}
        </div>

        {message.voice ? (
          <>
            <div className="mt-1 flex items-center gap-2">
              <span className="grid size-5 shrink-0 place-items-center rounded-full bg-wellbeing">
                <Play className="size-2 fill-white text-white dark:fill-background dark:text-background" />
              </span>
              <span className="flex h-4 flex-1 items-center gap-px" aria-hidden>
                {WAVE.map((h, i) => (
                  <i
                    key={i}
                    className="block w-px rounded-full bg-wellbeing/55"
                    style={{ height: `${h}px` }}
                  />
                ))}
              </span>
              <span className="font-mono text-[9px] text-muted-foreground tabular-nums">
                {message.voice.duration}
              </span>
            </div>
            <p className="mt-1 text-[13px] leading-snug">
              {message.voice.transcript}
            </p>
          </>
        ) : (
          <p className="text-[13px] leading-snug">{message.text}</p>
        )}

        {message.attachment && (
          <div className="mt-1.5 flex items-center gap-2 rounded-lg bg-card px-2 py-1.5">
            <span
              className={cn(
                "grid size-6 shrink-0 place-items-center rounded-md",
                message.attachment.kind === "photo"
                  ? "bg-symptom/15 text-symptom"
                  : "bg-admin/15 text-admin",
              )}
            >
              {message.attachment.kind === "photo" ? (
                <ImageIcon className="size-3" />
              ) : (
                <FileText className="size-3" />
              )}
            </span>
            <span className="min-w-0 leading-tight">
              <span className="block text-[11px] font-semibold">
                {message.attachment.label}
              </span>
              <span className="block text-[10px] text-muted-foreground">
                {message.attachment.note}
              </span>
            </span>
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto h-5 px-2 text-[11px] text-penny"
            >
              {message.attachment.action}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
