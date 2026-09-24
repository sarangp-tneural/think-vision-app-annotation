import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
  DialogTrigger, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Plus, Image as ImageIcon, Trash2, ArrowRight, Boxes, Users } from "lucide-react";
import { toast } from "sonner";

export default function Dashboard() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState([]);
  const [teams, setTeams] = useState([]);
  const [filterTeam, setFilterTeam] = useState("all");
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [taskType, setTaskType] = useState("object_detection");
  const [teamId, setTeamId] = useState("");
  const [creating, setCreating] = useState(false);

  const load = async () => {
    try {
      const [pRes, tRes] = await Promise.all([
        api.get("/projects"),
        api.get("/teams"),
      ]);
      setProjects(pRes.data);
      setTeams(tRes.data);
      if (tRes.data.length > 0 && !teamId) {
        const personal = tRes.data.find((t) => t.is_personal);
        setTeamId(personal ? personal.id : tRes.data[0].id);
      }
    } catch (e) {
      toast.error("Failed to load projects");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  const createProject = async () => {
    if (!name.trim()) return toast.error("Name required");
    setCreating(true);
    try {
      const { data } = await api.post("/projects", { name, description, task_type: taskType, team_id: teamId || undefined });
      toast.success("Project created");
      setOpen(false);
      setName(""); setDescription(""); setTaskType("object_detection");
      navigate(`/projects/${data.id}`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Create failed");
    } finally {
      setCreating(false);
    }
  };

  const deleteProject = async (id, e) => {
    e.stopPropagation();
    if (!window.confirm("Delete this project and all its data?")) return;
    try {
      await api.delete(`/projects/${id}`);
      toast.success("Deleted");
      setProjects(projects.filter((p) => p.id !== id));
    } catch (e) {
      toast.error("Delete failed");
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[{ label: "Projects" }]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="flex items-end justify-between mb-12">
          <div>
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Workspace</div>
            <h1 className="font-heading text-4xl md:text-5xl font-bold tracking-tight mb-2">Projects</h1>
            <p className="text-sm text-muted-foreground">
              {projects.length} {projects.length === 1 ? "project" : "projects"} · Managed by you
            </p>
          </div>

          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button
                className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-11 px-6 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="new-project-btn"
              >
                <Plus className="w-4 h-4 mr-2" /> New Project
              </Button>
            </DialogTrigger>
            <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm">
              <DialogHeader>
                <DialogTitle className="font-heading tracking-tight">Create new project</DialogTitle>
              </DialogHeader>
              <div className="space-y-4 py-2">
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Name</Label>
                  <Input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="traffic-detection"
                    className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                    data-testid="project-name-input"
                  />
                </div>
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Description</Label>
                  <Textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Detect vehicles in urban intersections..."
                    className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                    data-testid="project-desc-input"
                  />
                </div>
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Task type</Label>
                  <Select value={taskType} onValueChange={setTaskType}>
                    <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="project-tasktype-select">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                      <SelectItem value="object_detection">Object Detection</SelectItem>
                      <SelectItem value="classification">Classification</SelectItem>
                      <SelectItem value="segmentation">Instance Segmentation</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <DialogFooter>
                <Button
                  onClick={createProject}
                  disabled={creating}
                  className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                  data-testid="project-create-submit-btn"
                >
                  {creating ? "Creating..." : "Create Project"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>

        {loading ? (
          <div className="text-muted-foreground text-sm">Loading...</div>
        ) : projects.length === 0 ? (
          <div className="border border-dashed border-[#27272A] p-16 text-center" data-testid="empty-state">
            <Boxes className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
            <h3 className="font-heading text-xl mb-2">No projects yet</h3>
            <p className="text-sm text-muted-foreground mb-6">Create your first project to start building vision datasets.</p>
            <Button
              onClick={() => setOpen(true)}
              className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
              data-testid="empty-new-project-btn"
            >
              <Plus className="w-4 h-4 mr-2" /> New Project
            </Button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-0 border-l border-t border-[#27272A]">
            {projects.filter((p) => filterTeam === "all" || p.team_id === filterTeam).map((p) => (
              <div
                key={p.id}
                onClick={() => navigate(`/projects/${p.id}`)}
                className="border-r border-b border-[#27272A] p-6 cursor-pointer group panel-hover"
                data-testid={`project-card-${p.id}`}
              >
                <div className="flex items-start justify-between mb-6">
                  <div className="w-10 h-10 bg-[#1C1C1C] border border-[#27272A] flex items-center justify-center">
                    <ImageIcon className="w-4 h-4 text-primary" strokeWidth={1.5} />
                  </div>
                  <button
                    onClick={(e) => deleteProject(p.id, e)}
                    className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive transition-opacity"
                    data-testid={`project-delete-${p.id}`}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
                <div className="text-[10px] uppercase tracking-[0.2em] text-primary mb-2 flex items-center gap-2">
                  <span>{p.task_type.replace("_", " ")}</span>
                  {p.team_name && (
                    <>
                      <span className="text-[#3f3f46]">/</span>
                      <span className="text-muted-foreground truncate flex items-center gap-1">
                        <Users className="w-3 h-3" /> {p.team_name}
                      </span>
                    </>
                  )}
                </div>
                <h3 className="font-heading text-lg font-medium mb-2 group-hover:text-primary transition-colors">{p.name}</h3>
                <p className="text-xs text-muted-foreground mb-6 line-clamp-2 min-h-[32px]">
                  {p.description || "No description"}
                </p>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.2em] text-muted-foreground pt-4 border-t border-[#27272A]">
                  <span>{p.image_count} imgs</span>
                  <span>{p.annotated_count} labeled</span>
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
