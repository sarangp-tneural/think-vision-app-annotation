import { useEffect, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { toast } from "sonner";
import {
  Users, TrendingUp, Image as ImageIcon, CheckCircle2, Clock, XCircle, Send, Layers,
} from "lucide-react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from "recharts";

const STATUS_COLORS = {
  unassigned: "#71717A",
  assigned: "#06B6D4",
  submitted: "#EAB308",
  approved: "#22C55E",
  rejected: "#EF4444",
};

export default function TeamDashboard() {
  const { tid } = useParams();
  const navigate = useNavigate();
  const [team, setTeam] = useState(null);
  const [dash, setDash] = useState(null);
  const [projectStats, setProjectStats] = useState({}); // pid -> project_dashboard result
  const [selectedPid, setSelectedPid] = useState(null);

  const load = async () => {
    try {
      const [t, d] = await Promise.all([
        api.get(`/teams/${tid}`),
        api.get(`/teams/${tid}/dashboard`),
      ]);
      setTeam(t.data);
      setDash(d.data);
      if (d.data.projects.length > 0 && !selectedPid) {
        setSelectedPid(d.data.projects[0].id);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load dashboard");
      navigate(`/teams/${tid}`);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [tid]);

  useEffect(() => {
    if (!selectedPid || projectStats[selectedPid]) return;
    (async () => {
      try {
        const { data } = await api.get(`/projects/${selectedPid}/dashboard`);
        setProjectStats((prev) => ({ ...prev, [selectedPid]: data }));
      } catch (err) { console.debug("project stats load failed", err); }
    })();
  }, [selectedPid, projectStats]);

  if (!team || !dash) {
    return <div className="min-h-screen bg-background"><Navbar /></div>;
  }

  const currentProject = selectedPid ? projectStats[selectedPid] : null;

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[
        { label: "Teams", to: "/teams" },
        { label: team.name, to: `/teams/${tid}` },
        { label: "Dashboard" },
      ]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="flex items-end justify-between mb-10">
          <div>
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Team Analytics</div>
            <h1 className="font-heading text-4xl font-bold tracking-tight mb-2">Dashboard</h1>
            <p className="text-sm text-muted-foreground">{team.name} · Real-time annotation progress</p>
          </div>
        </div>

        {/* Team overview */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-0 border-l border-t border-[#27272A] mb-10">
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Projects</div>
            <div className="font-heading text-3xl font-bold" data-testid="team-stat-projects">{dash.total_projects}</div>
          </div>
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Total Images</div>
            <div className="font-heading text-3xl font-bold" data-testid="team-stat-images">{dash.total_images}</div>
          </div>
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Annotated</div>
            <div className="font-heading text-3xl font-bold text-primary" data-testid="team-stat-annotated">{dash.total_annotated}</div>
          </div>
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Team Members</div>
            <div className="font-heading text-3xl font-bold">{team.members.filter((m) => m.status === "active").length}</div>
          </div>
        </div>

        {/* Projects breakdown */}
        {dash.projects.length > 0 && (
          <div className="mb-10">
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// Projects Progress</div>
            <div className="panel p-6">
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={dash.projects}>
                  <XAxis dataKey="name" stroke="#A1A1AA" style={{ fontFamily: "JetBrains Mono", fontSize: 10 }} />
                  <YAxis stroke="#A1A1AA" style={{ fontFamily: "JetBrains Mono", fontSize: 10 }} />
                  <Tooltip contentStyle={{ background: "#121212", border: "1px solid #27272A", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 11 }} />
                  <Legend wrapperStyle={{ fontFamily: "JetBrains Mono", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.1em" }} />
                  <Bar dataKey="images" fill="#27272A" name="Total" />
                  <Bar dataKey="annotated" fill="#06B6D4" name="Annotated" />
                  <Bar dataKey="approved" fill="#22C55E" name="Approved" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Project selector */}
        {dash.projects.length > 0 && (
          <div className="mb-6">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3">Select project for detailed view</div>
            <div className="flex flex-wrap gap-2">
              {dash.projects.map((p) => (
                <button
                  key={p.id}
                  onClick={() => setSelectedPid(p.id)}
                  className={`px-3 py-2 border text-xs transition-colors ${
                    selectedPid === p.id ? "border-primary bg-[#1C1C1C] text-primary" : "border-[#27272A] hover:bg-[#1C1C1C]"
                  }`}
                  data-testid={`project-select-${p.id}`}
                >
                  {p.name} <span className="text-muted-foreground">· {p.completion_pct}%</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Project detail dashboard */}
        {currentProject && (
          <>
            {/* Status distribution */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-10">
              <div className="panel p-6 lg:col-span-2">
                <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// Status Breakdown</div>
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie
                      data={Object.entries(currentProject.counts).map(([k, v]) => ({ name: k, value: v }))}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      outerRadius={80}
                      label={({ name, value }) => value > 0 ? `${name}: ${value}` : ""}
                      style={{ fontFamily: "JetBrains Mono", fontSize: 10 }}
                    >
                      {Object.keys(currentProject.counts).map((k) => (
                        <Cell key={k} fill={STATUS_COLORS[k]} />
                      ))}
                    </Pie>
                    <Tooltip contentStyle={{ background: "#121212", border: "1px solid #27272A", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 11 }} />
                  </PieChart>
                </ResponsiveContainer>
              </div>

              <div className="panel p-6 flex flex-col justify-between">
                <div>
                  <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// Progress</div>
                  {[
                    { label: "Annotation", pct: currentProject.annotation_pct, color: "#06B6D4" },
                    { label: "Review", pct: currentProject.review_pct, color: "#EAB308" },
                    { label: "Approval", pct: currentProject.completion_pct, color: "#22C55E" },
                  ].map((p) => (
                    <div key={p.label} className="mb-4">
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="text-muted-foreground uppercase tracking-[0.2em]">{p.label}</span>
                        <span className="font-bold">{p.pct}%</span>
                      </div>
                      <div className="h-1.5 bg-[#27272A]">
                        <div className="h-full transition-all" style={{ width: `${p.pct}%`, background: p.color }} data-testid={`progress-${p.label.toLowerCase()}`} />
                      </div>
                    </div>
                  ))}
                </div>
                <Link
                  to={`/projects/${selectedPid}`}
                  className="text-xs uppercase tracking-[0.2em] text-primary hover:underline mt-4 flex items-center gap-1"
                >
                  Open project →
                </Link>
              </div>
            </div>

            {/* Member performance */}
            <div>
              <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// Team Performance</div>
              <div className="border-t border-[#27272A]">
                <div className="grid grid-cols-12 gap-2 text-[10px] uppercase tracking-[0.2em] text-muted-foreground border-b border-[#27272A] p-3">
                  <div className="col-span-3">Member</div>
                  <div className="col-span-2">Role</div>
                  <div className="col-span-1 text-right">Assigned</div>
                  <div className="col-span-2 text-right">Submitted</div>
                  <div className="col-span-1 text-right">Approved</div>
                  <div className="col-span-1 text-right">Rejected</div>
                  <div className="col-span-2 text-right">Last Active</div>
                </div>
                {currentProject.members_perf.map((m) => (
                  <div key={m.user_id} className="grid grid-cols-12 gap-2 text-xs border-b border-[#27272A] p-3 items-center panel-hover" data-testid={`member-perf-${m.user_id}`}>
                    <div className="col-span-3">
                      <div className="font-medium">{m.name}</div>
                      <div className="text-[10px] text-muted-foreground truncate">{m.email}</div>
                    </div>
                    <div className="col-span-2 text-[10px] uppercase tracking-[0.2em] text-primary">{m.role}</div>
                    <div className="col-span-1 text-right font-bold">{m.assigned_images}</div>
                    <div className="col-span-2 text-right text-[#EAB308]">{m.submitted}</div>
                    <div className="col-span-1 text-right text-[#22C55E]">{m.approved}</div>
                    <div className="col-span-1 text-right text-destructive">{m.rejected}</div>
                    <div className="col-span-2 text-right text-[10px] text-muted-foreground">
                      {m.last_active ? new Date(m.last_active).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"}
                    </div>
                  </div>
                ))}
                {currentProject.members_perf.length === 0 && (
                  <div className="text-center text-xs text-muted-foreground p-8">No team members yet</div>
                )}
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
