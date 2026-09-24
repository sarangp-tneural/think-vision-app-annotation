import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter,
} from "@/components/ui/dialog";
import { toast } from "sonner";
import { Users, Plus, Crown, ArrowRight } from "lucide-react";

export default function Teams() {
  const navigate = useNavigate();
  const [teams, setTeams] = useState([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  const load = async () => {
    try {
      const { data } = await api.get("/teams");
      setTeams(data);
    } catch { toast.error("Failed to load teams"); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const createTeam = async () => {
    if (!name.trim()) return toast.error("Name required");
    setCreating(true);
    try {
      const { data } = await api.post("/teams", { name });
      toast.success("Team created");
      setOpen(false);
      setName("");
      navigate(`/teams/${data.id}`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Create failed");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[{ label: "Teams" }]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="flex items-end justify-between mb-12">
          <div>
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Collaborate</div>
            <h1 className="font-heading text-4xl md:text-5xl font-bold tracking-tight mb-2">Teams</h1>
            <p className="text-sm text-muted-foreground">
              Workspaces to collaborate on datasets with your team.
            </p>
          </div>

          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button
                className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-11 px-6 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="new-team-btn"
              >
                <Plus className="w-4 h-4 mr-2" /> New Team
              </Button>
            </DialogTrigger>
            <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm">
              <DialogHeader>
                <DialogTitle className="font-heading tracking-tight">Create new team</DialogTitle>
              </DialogHeader>
              <div className="space-y-4 py-2">
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Team name</Label>
                  <Input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Acme ML Team"
                    className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                    data-testid="team-name-input"
                  />
                </div>
              </div>
              <DialogFooter>
                <Button
                  onClick={createTeam}
                  disabled={creating}
                  className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                  data-testid="team-create-submit-btn"
                >
                  {creating ? "Creating..." : "Create Team"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>

        {loading ? (
          <div className="text-muted-foreground text-sm">Loading...</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-0 border-l border-t border-[#27272A]">
            {teams.map((t) => (
              <div
                key={t.id}
                onClick={() => navigate(`/teams/${t.id}`)}
                className="border-r border-b border-[#27272A] p-6 cursor-pointer group panel-hover"
                data-testid={`team-card-${t.id}`}
              >
                <div className="flex items-start justify-between mb-6">
                  <div className="w-10 h-10 bg-[#1C1C1C] border border-[#27272A] flex items-center justify-center">
                    <Users className="w-4 h-4 text-primary" strokeWidth={1.5} />
                  </div>
                  {t.role === "owner" && (
                    <div className="flex items-center gap-1 text-[10px] uppercase tracking-[0.2em] text-primary">
                      <Crown className="w-3 h-3" /> Owner
                    </div>
                  )}
                  {t.role === "admin" && (
                    <div className="text-[10px] uppercase tracking-[0.2em] text-[#D946EF]">Admin</div>
                  )}
                  {t.role === "member" && (
                    <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Member</div>
                  )}
                </div>
                <div className="text-[10px] uppercase tracking-[0.2em] text-primary mb-2">
                  {t.is_personal ? "Personal" : "Team"}
                </div>
                <h3 className="font-heading text-lg font-medium mb-2 group-hover:text-primary transition-colors">{t.name}</h3>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.2em] text-muted-foreground pt-4 border-t border-[#27272A] mt-4">
                  <span>{t.member_count} member{t.member_count !== 1 ? "s" : ""}</span>
                  <span>{t.project_count} project{t.project_count !== 1 ? "s" : ""}</span>
                  <ArrowRight className="w-3 h-3 text-primary opacity-0 group-hover:opacity-100 transition-opacity" />
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
