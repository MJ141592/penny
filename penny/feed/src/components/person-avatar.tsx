import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { cn } from "@/lib/utils";
import { people, personBg, type PersonId } from "@/lib/data";

const sizes = {
  xs: "size-5 text-[9px]",
  sm: "size-7 text-[11px]",
  md: "size-9 text-xs",
  lg: "size-11 text-sm",
} as const;

export function PersonAvatar({
  id,
  size = "sm",
  className,
}: {
  id: PersonId;
  size?: keyof typeof sizes;
  className?: string;
}) {
  const person = people[id];
  return (
    <Avatar className={cn(sizes[size], className)}>
      <AvatarFallback
        className={cn(
          personBg[id],
          "font-semibold text-white dark:text-background",
        )}
      >
        {person.initials}
      </AvatarFallback>
    </Avatar>
  );
}
