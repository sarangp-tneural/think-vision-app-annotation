import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, getToken, API } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { GitBranch, Download, Plus } from "lucide-react";

export default function Versions() {
  const { pid } = useParams();
  const navigate = useNavigate();
  const [project, setProject] = useState(null);
  const [versions, setVersions] = useState([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [creating, setCreating] = useState(false);
  const [exporting, setExporting] = useState(null);
  const [format, setFormat] = useState("yolo");

  const load = async () => {
    try {
      const [p, v] = await Promise.all([
        api.get(`/projects/${pid}`),
        api.get(`/projects/${pid}/versions`),
      ]);
      setProject(p.data);
      setVersions(v.data);
    } catch { toast.error("Failed to load"); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [pid]);

  const createVersion = async () => {
    if (!name.trim()) return toast.error("Name required");
    setCreating(true);
    try {
      await api.post(`/projects/${pid}/versions`, { name, notes });
      toast.success("Version created");
      setOpen(false);
      setName(""); setNotes("");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Create failed");
    } finally {
      setCreating(false);
    }
  };

  const download = async (fmt) => {
    setExporting(fmt);
    try {
      const token = getToken();
      const res = await fetch(`${API}/projects/${pid}/export?format=${fmt}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error("Export failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${project.name}_${fmt}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(`Exported ${fmt.toUpperCase()}`);
    } catch (e) {
      toast.error("Export failed");
    } finally {
      setExporting(null);
    }
  };

  if (!project) return <div className="min-h-screen bg-background"><Navbar /></div>;

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[
        { label: "Projects", to: "/dashboard" },
        { label: project.name, to: `/projects/${pid}` },
        { label: "Versions" },
      ]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="flex items-end justify-between mb-10">
          <div>
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Datasets</div>
            <h1 className="font-heading text-4xl font-bold tracking-tight mb-2">Versions & Export</h1>
            <p className="text-sm text-muted-foreground">
              Snapshot your annotated data, then export to your preferred format.
            </p>
          </div>

          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button
                className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-11 px-6 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="new-version-btn"
              >
                <Plus className="w-4 h-4 mr-2" /> New Version
              </Button>
            </DialogTrigger>
            <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm">
              <DialogHeader>
                <DialogTitle className="font-heading tracking-tight">Create version snapshot</DialogTitle>
              </DialogHeader>
              <div className="space-y-4 py-2">
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Version name</Label>
                  <Input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="v1.0-initial"
                    className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                    data-testid="version-name-input"
                  />
                </div>
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Notes</Label>
                  <Textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="What changed in this version?"
                    className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                    data-testid="version-notes-input"
                  />
                </div>
                <div className="text-xs text-muted-foreground">
                  This will snapshot <span className="text-primary">{project.annotated_count}</span> annotated images.
                </div>
              </div>
              <DialogFooter>
                <Button
                  onClick={createVersion}
                  disabled={creating}
                  className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                  data-testid="version-submit-btn"
                >
                  {creating ? "Creating..." : "Create Snapshot"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>

        {/* Export panel */}
        <div className="panel p-6 mb-10">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Export current dataset</div>
          <div className="flex flex-wrap items-end gap-4">
            <div className="flex-1 min-w-[200px]">
              <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Format</Label>
              <Select value={format} onValueChange={setFormat}>
                <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="export-format-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                  <SelectItem value="yolo">YOLO (Ultralytics)</SelectItem>
                  <SelectItem value="yolo_obb">YOLO OBB (Rotated)</SelectItem>
                  <SelectItem value="coco">COCO JSON</SelectItem>
                  <SelectItem value="voc">Pascal VOC (XML)</SelectItem>
                  <SelectItem value="split">YOLO (train/valid/test split)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button
              onClick={() => download(format)}
              disabled={exporting || project.annotated_count === 0}
              className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 px-6 text-xs uppercase tracking-[0.2em] font-bold"
              data-testid="export-download-btn"
            >
              <Download className="w-4 h-4 mr-2" /> {exporting === format ? "Zipping..." : "Download Zip"}
            </Button>
          </div>
          {project.annotated_count === 0 && (
            <div className="text-xs text-muted-foreground mt-4">
              No annotated images yet. Label some images first.
            </div>
          )}
        </div>

        {/* Versions list */}
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">
            History ({versions.length})
          </div>
          {versions.length === 0 ? (
            <div className="border border-dashed border-[#27272A] p-16 text-center">
              <GitBranch className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
              <h3 className="font-heading text-xl mb-2">No versions yet</h3>
              <p className="text-sm text-muted-foreground">Create your first snapshot to lock in dataset state.</p>
            </div>
          ) : (
            <div className="border-t border-[#27272A]">
              {versions.map((v, i) => (
                <div
                  key={v.id}
                  className="border-b border-[#27272A] p-5 flex flex-wrap items-center justify-between gap-4 panel-hover"
                  data-testid={`version-row-${v.id}`}
                >
                  <div className="flex items-center gap-4">
                    <div className="w-8 h-8 bg-[#1C1C1C] border border-[#27272A] flex items-center justify-center">
                      <GitBranch className="w-4 h-4 text-primary" />
                    </div>
                    <div>
                      <div className="font-heading font-medium">{v.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {v.image_count} images · {new Date(v.created_at).toLocaleString()}
                      </div>
                      {v.notes && <div className="text-xs text-muted-foreground mt-1 max-w-lg">{v.notes}</div>}
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => navigate(`/projects/${pid}/deploy?version=${v.id}`)}
                    className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                    data-testid={`train-version-${v.id}`}
                  >
                    Train Model →
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
