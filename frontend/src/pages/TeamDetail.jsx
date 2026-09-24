import { useEffect, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "sonner";
import { Users, Plus, Mail, UserMinus, Shield, MoreVertical, Trash2, Crown, LayoutDashboard, Eye, PenTool } from "lucide-react";

const ROLE_LABELS = {
  owner: { label: "Owner", color: "text-primary", icon: Crown },
  admin: { label: "Admin", color: "text-[#D946EF]", icon: Shield },
  reviewer: { label: "Reviewer", color: "text-[#EAB308]", icon: Eye },
  annotator: { label: "Annotator", color: "text-[#22C55E]", icon: PenTool },
  member: { label: "Member", color: "text-muted-foreground", icon: Users },
};

export default function TeamDetail() {
  const { tid } = useParams();
  const navigate = useNavigate();
  const [team, setTeam] = useState(null);
  const [teams, setTeams] = useState([]);
  const [loading, setLoading] = useState(true);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [inviting, setInviting] = useState(false);

  const load = async () => {
    try {
      const [t, list] = await Promise.all([
        api.get(`/teams/${tid}`),
        api.get(`/teams`),
      ]);
      setTeam(t.data);
      setTeams(list.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load team");
      navigate("/dashboard");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [tid]);

  const myMembership = team?.members.find((m) => m.user_id && m.status === "active" && teams.find((t) => t.id === team.id)?.role === m.role);
  const myRole = teams.find((t) => t.id === tid)?.role;
  const canManage = myRole === "owner" || myRole === "admin";

  const invite = async () => {
    if (!email.trim()) return toast.error("Email required");
    setInviting(true);
    try {
      const { data } = await api.post(`/teams/${tid}/invite`, { email, role });
      toast.success(data.status === "active" ? `Added ${email}` : `Invite sent to ${email}`);
      setEmail("");
      setInviteOpen(false);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Invite failed");
    } finally {
      setInviting(false);
    }
  };

  const removeMember = async (mid) => {
    if (!window.confirm("Remove this member?")) return;
    try {
      await api.delete(`/teams/${tid}/members/${mid}`);
      toast.success("Member removed");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Remove failed");
    }
  };

  const changeRole = async (mid, newRole) => {
    try {
      await api.patch(`/teams/${tid}/members/${mid}`, { role: newRole });
      toast.success("Role updated");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Update failed");
    }
  };

  const deleteTeam = async () => {
    if (!window.confirm(`Permanently delete "${team.name}" and all its projects?`)) return;
    try {
      await api.delete(`/teams/${tid}`);
      toast.success("Team deleted");
      navigate("/dashboard");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  if (loading || !team) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <div className="max-w-[1600px] mx-auto px-6 py-16 text-muted-foreground text-sm">Loading team...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[{ label: "Teams", to: "/teams" }, { label: team.name }]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="flex flex-wrap items-end justify-between gap-6 mb-10">
          <div>
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">
              // {team.is_personal ? "Personal workspace" : "Team workspace"}
            </div>
            <h1 className="font-heading text-4xl font-bold tracking-tight mb-2" data-testid="team-name">{team.name}</h1>
            <p className="text-sm text-muted-foreground">
              {team.members.filter((m) => m.status === "active").length} active members · created {new Date(team.created_at).toLocaleDateString()}
            </p>
          </div>

          <div className="flex items-center gap-3">
            {(myRole === "owner" || myRole === "admin" || myRole === "reviewer") && (
              <Button
                variant="outline"
                onClick={() => navigate(`/teams/${tid}/dashboard`)}
                className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                data-testid="team-dashboard-btn"
              >
                <LayoutDashboard className="w-4 h-4 mr-2" /> Dashboard
              </Button>
            )}
            {canManage && (
              <Dialog open={inviteOpen} onOpenChange={setInviteOpen}>
                <DialogTrigger asChild>
                  <Button
                    className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-11 px-6 text-xs uppercase tracking-[0.2em] font-bold"
                    data-testid="invite-member-btn"
                  >
                    <Mail className="w-4 h-4 mr-2" /> Invite Member
                  </Button>
                </DialogTrigger>
                <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm">
                  <DialogHeader>
                    <DialogTitle className="font-heading tracking-tight">Invite to {team.name}</DialogTitle>
                  </DialogHeader>
                  <div className="space-y-4 py-2">
                    <div>
                      <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Email</Label>
                      <Input
                        type="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        placeholder="teammate@company.com"
                        className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                        data-testid="invite-email-input"
                      />
                    </div>
                    <div>
                      <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Role</Label>
                      <Select value={role} onValueChange={setRole}>
                        <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="invite-role-select">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                          <SelectItem value="annotator">Annotator — label images only</SelectItem>
                          <SelectItem value="reviewer">Reviewer — review + approve/reject</SelectItem>
                          <SelectItem value="member">Member — annotate + view</SelectItem>
                          <SelectItem value="admin">Admin — full access + manage members</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      If they already have an account, they'll be added immediately. Otherwise the invite activates when they sign up with this email.
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={invite}
                      disabled={inviting}
                      className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                      data-testid="invite-submit-btn"
                    >
                      {inviting ? "Sending..." : "Send Invite"}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
            {myRole === "owner" && !team.is_personal && (
              <Button
                variant="outline"
                onClick={deleteTeam}
                className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-destructive hover:text-destructive text-xs uppercase tracking-[0.2em]"
                data-testid="delete-team-btn"
              >
                <Trash2 className="w-4 h-4 mr-2" /> Delete Team
              </Button>
            )}
          </div>
        </div>

        {/* Members table */}
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3">
            Members ({team.members.length})
          </div>
          <div className="border-t border-[#27272A]">
            {team.members.map((m) => {
              const roleMeta = ROLE_LABELS[m.role] || ROLE_LABELS.member;
              const RoleIcon = roleMeta.icon;
              return (
                <div
                  key={m.id}
                  className="border-b border-[#27272A] p-4 flex items-center justify-between panel-hover"
                  data-testid={`member-row-${m.id}`}
                >
                  <div className="flex items-center gap-4">
                    <div className="w-9 h-9 bg-[#1C1C1C] border border-[#27272A] flex items-center justify-center text-primary text-sm font-bold">
                      {(m.name || m.email || "?").charAt(0).toUpperCase()}
                    </div>
                    <div>
                      <div className="font-heading font-medium text-sm">
                        {m.name || m.email}
                        {m.status === "pending" && (
                          <span className="ml-2 text-[10px] uppercase tracking-[0.2em] text-[#EAB308]">· pending</span>
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground">{m.email}</div>
                    </div>
                  </div>

                  <div className="flex items-center gap-4">
                    <div className={`flex items-center gap-2 text-xs uppercase tracking-[0.2em] ${roleMeta.color}`}>
                      <RoleIcon className="w-3 h-3" /> {roleMeta.label}
                    </div>
                    {myRole === "owner" && m.role !== "owner" && (
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <button className="text-muted-foreground hover:text-primary transition-colors" data-testid={`member-menu-${m.id}`}>
                            <MoreVertical className="w-4 h-4" />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent className="bg-[#121212] border-[#27272A] rounded-sm">
                          {["admin", "annotator", "reviewer", "member"].filter((r) => r !== m.role).map((r) => (
                            <DropdownMenuItem key={r} onClick={() => changeRole(m.id, r)} data-testid={`set-role-${r}-${m.id}`}>
                              <Shield className="w-4 h-4 mr-2" /> Set as {r.charAt(0).toUpperCase() + r.slice(1)}
                            </DropdownMenuItem>
                          ))}
                          <DropdownMenuItem
                            onClick={() => removeMember(m.id)}
                            className="text-destructive focus:text-destructive"
                            data-testid={`remove-member-${m.id}`}
                          >
                            <UserMinus className="w-4 h-4 mr-2" /> Remove
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </main>
    </div>
  );
}
