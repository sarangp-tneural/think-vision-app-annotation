import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { toast } from "sonner";
import { Activity as ActivityIcon, UserPlus, UserMinus, Image as ImageIcon, CheckCircle2, XCircle, Send, MessageSquare, Sparkles, GitBranch } from "lucide-react";

const ACTION_ICONS = {
  project_assigned: UserPlus,
  project_unassigned: UserMinus,
  image_assigned: ImageIcon,
  image_unassigned: ImageIcon,
  image_submitted: Send,
  image_approved: CheckCircle2,
  image_approved_d: CheckCircle2,
  image_rejected: XCircle,
  image_rejected_d: XCircle,
  bulk_assigned: UserPlus,
  comment_added: MessageSquare,
  auto_label: Sparkles,
  version_created: GitBranch,
};

export default function Activity() {
  const { pid } = useParams();
  const [items, setItems] = useState([]);
  const [project, setProject] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const [p, a] = await Promise.all([
          api.get(`/projects/${pid}`),
          api.get(`/projects/${pid}/activity`),
        ]);
        setProject(p.data);
        setItems(a.data);
      } catch (e) {
        toast.error(e.response?.data?.detail || "Failed to load");
      }
    })();
  }, [pid]);

  const humanize = (a) => a.replace(/_/g, " ").replace(/\bd\b$/, "").trim();

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={project ? [
        { label: "Projects", to: "/dashboard" },
        { label: project.name, to: `/projects/${pid}` },
        { label: "Activity" },
      ] : []} />

      <main className="max-w-[1200px] mx-auto px-6 py-12">
        <div className="mb-10">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Audit log</div>
          <h1 className="font-heading text-4xl font-bold tracking-tight mb-2">Activity</h1>
          <p className="text-sm text-muted-foreground">{items.length} entries · project audit history</p>
        </div>

        {items.length === 0 ? (
          <div className="border border-dashed border-[#27272A] p-16 text-center">
            <ActivityIcon className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
            <h3 className="font-heading text-xl mb-2">No activity yet</h3>
          </div>
        ) : (
          <div className="border-t border-[#27272A]">
            {items.map((it) => {
              const Icon = ACTION_ICONS[it.action] || ActivityIcon;
              return (
                <div key={it.id} className="border-b border-[#27272A] p-4 flex items-start gap-4 panel-hover" data-testid={`activity-${it.id}`}>
                  <div className="w-8 h-8 bg-[#1C1C1C] border border-[#27272A] flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Icon className="w-4 h-4 text-primary" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm">
                      <span className="font-medium">{it.user_name}</span>
                      <span className="text-muted-foreground"> · {humanize(it.action)}</span>
                    </div>
                    {Object.keys(it.meta || {}).length > 0 && (
                      <div className="text-[10px] text-muted-foreground mt-1 font-mono">
                        {Object.entries(it.meta).map(([k, v]) => (
                          <span key={k} className="mr-3">{k}={typeof v === "object" ? JSON.stringify(v) : String(v).slice(0, 40)}</span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground flex-shrink-0">
                    {new Date(it.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
