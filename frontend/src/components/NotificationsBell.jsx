import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Bell, CheckCheck } from "lucide-react";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export default function NotificationsBell() {
  const [items, setItems] = useState([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);

  const load = async () => {
    try {
      const { data } = await api.get("/notifications");
      setItems(data.items || []);
      setUnread(data.unread || 0);
    } catch (err) { console.debug("notifications load failed", err); }
  };

  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, []);

  const markAll = async () => {
    try {
      await api.post("/notifications/mark-all-read");
      load();
    } catch (err) { console.debug("mark all read failed", err); }
  };

  const clickNotif = async (n) => {
    try {
      await api.post(`/notifications/${n.id}/read`);
      load();
    } catch (err) { console.debug("mark read failed", err); }
    setOpen(false);
  };

  const timeAgo = (ts) => {
    const s = Math.round((Date.now() - new Date(ts).getTime()) / 1000);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
  };

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button className="relative text-muted-foreground hover:text-primary transition-colors" data-testid="notifications-bell">
          <Bell className="w-4 h-4" />
          {unread > 0 && (
            <span className="absolute -top-1 -right-2 bg-primary text-black text-[9px] font-bold px-1 min-w-[14px] h-[14px] flex items-center justify-center" data-testid="unread-badge">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="bg-[#121212] border-[#27272A] rounded-sm w-80 p-0 max-h-[500px] overflow-y-auto">
        <div className="flex items-center justify-between p-3 border-b border-[#27272A]">
          <span className="text-[10px] uppercase tracking-[0.2em] text-primary">// Notifications</span>
          {items.length > 0 && (
            <button onClick={markAll} className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground hover:text-primary flex items-center gap-1" data-testid="mark-all-read-btn">
              <CheckCheck className="w-3 h-3" /> Mark all
            </button>
          )}
        </div>
        {items.length === 0 ? (
          <div className="p-6 text-center text-xs text-muted-foreground">No notifications</div>
        ) : (
          items.slice(0, 20).map((n) => {
            const to = n.project_id ? (n.image_id ? `/projects/${n.project_id}/annotate/${n.image_id}` : `/projects/${n.project_id}`) : "/dashboard";
            return (
              <Link
                key={n.id}
                to={to}
                onClick={() => clickNotif(n)}
                className={`block p-3 border-b border-[#27272A] hover:bg-[#1C1C1C] transition-colors ${!n.read ? "bg-[#0f1a1c]" : ""}`}
                data-testid={`notif-${n.id}`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="text-xs">{n.title}</div>
                    <div className="text-[10px] text-muted-foreground uppercase tracking-[0.2em] mt-1">
                      {n.kind.replace("_", " ")} · {timeAgo(n.created_at)}
                    </div>
                  </div>
                  {!n.read && <span className="w-2 h-2 bg-primary flex-shrink-0 mt-1" />}
                </div>
              </Link>
            );
          })
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
