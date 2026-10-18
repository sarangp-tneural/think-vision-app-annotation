import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { avatarUrl } from "@/lib/api";

const initials = (name) => (name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]?.toUpperCase()).join("") || "?";

export default function UserAvatar({ user, className = "", ...props }) {
  return (
    <Avatar className={className} {...props}>
      <AvatarImage src={avatarUrl(user) || undefined} alt={user?.name || "avatar"} className="object-cover" />
      <AvatarFallback className="bg-[#1C1C1C] text-primary text-xs font-semibold">{initials(user?.name)}</AvatarFallback>
    </Avatar>
  );
}
