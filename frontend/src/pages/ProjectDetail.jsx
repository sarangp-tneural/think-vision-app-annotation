import { useEffect, useState, useRef } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { api, fileUrl } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Upload, Sparkles, ImageIcon, GitBranch, Rocket, CheckCircle2, Trash2, Video, Film, Loader2, XCircle, Clock, Send, UserPlus, Activity as ActivityIcon, Settings, TrendingUp } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter,
} from "@/components/ui/dialog";

export default function ProjectDetail() {
  const { pid } = useParams();
  const navigate = useNavigate();
  const [project, setProject] = useState(null);
  const [images, setImages] = useState([]);
  const [videos, setVideos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [videoInterval, setVideoInterval] = useState(30);
  const [videoIntervalMode, setVideoIntervalMode] = useState("frames"); // "frames" | "seconds"
  const [bulkClassText, setBulkClassText] = useState("");
  const [bulkClassOpen, setBulkClassOpen] = useState(false);
  const [statusFilter, setStatusFilter] = useState("all");
  const [batchJob, setBatchJob] = useState(null);
  // Team assignment state
  const [members, setMembers] = useState([]);
  const [assignments, setAssignments] = useState([]);
  const [myRole, setMyRole] = useState(null);
  // Bulk-select state
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkUser, setBulkUser] = useState("");
  const [bulkPriority, setBulkPriority] = useState("");
  const [bulkNotes, setBulkNotes] = useState("");
  // Assign project dialog
  const [assignProjectOpen, setAssignProjectOpen] = useState(false);
  const [assignUserIds, setAssignUserIds] = useState(new Set());
  // Settings dialog
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState({ confidence_threshold: 0.4, fallback_to_gemini: true, min_boxes_threshold: 1, show_confidence: true, gemini_restrict_to_classes: false, gemini_allowed_classes: null });
  const [alQueue, setAlQueue] = useState(null);
  const [alLoading, setAlLoading] = useState(false);
  const fileInputRef = useRef(null);
  const videoInputRef = useRef(null);

  const load = async () => {
    try {
      const [pRes, iRes, vRes] = await Promise.all([
        api.get(`/projects/${pid}`),
        api.get(`/projects/${pid}/images`),
        api.get(`/projects/${pid}/videos`),
      ]);
      setProject(pRes.data);
      setImages(iRes.data);
      setVideos(vRes.data);
      if (pRes.data.settings) setSettings(pRes.data.settings);
      // Load team members + project assignments if project has a team
      if (pRes.data.team_id) {
        try {
          const [t, a] = await Promise.all([
            api.get(`/teams/${pRes.data.team_id}`),
            api.get(`/projects/${pid}/assignments`),
          ]);
          setMembers((t.data.members || []).filter((m) => m.status === "active"));
          setAssignments(a.data);
          const teams = await api.get("/teams");
          const myTeam = teams.data.find((x) => x.id === pRes.data.team_id);
          setMyRole(myTeam ? myTeam.role : null);
        } catch (err) { console.debug("team assignments load failed", err); }
      }
    } catch (e) {
      toast.error("Failed to load project");
      navigate("/dashboard");
    } finally {
      setLoading(false);
    }
  };

  const canManage = myRole === "owner" || myRole === "admin";

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [pid]);

  // Poll video statuses if any are processing
  useEffect(() => {
    const processing = videos.some((v) => v.status === "processing" || v.status === "queued");
    if (!processing) return;
    const iv = setInterval(() => load(), 3000);
    return () => clearInterval(iv);
    // eslint-disable-next-line
  }, [videos]);

  const uploadVideo = async (file) => {
    if (!file) return;
    setUploading(true);
    const fd = new FormData();
    fd.append("file", file);
    if (videoIntervalMode === "seconds") {
      fd.append("interval_seconds", String(videoInterval));
      fd.append("interval_frames", "30");
    } else {
      fd.append("interval_frames", String(videoInterval));
      fd.append("interval_seconds", "0");
    }
    try {
      await api.post(`/projects/${pid}/videos`, fd, { headers: { "Content-Type": "multipart/form-data" } });
      toast.success("Video queued for frame extraction");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Video upload failed");
    } finally {
      setUploading(false);
    }
  };

  const addBulkClasses = async () => {
    if (!bulkClassText.trim()) return;
    try {
      const { data } = await api.post(`/projects/${pid}/classes/bulk`, { text: bulkClassText });
      setProject({ ...project, classes: data.classes });
      toast.success(`Added ${data.added.length} class${data.added.length !== 1 ? "es" : ""}`);
      setBulkClassText("");
      setBulkClassOpen(false);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to add classes");
    }
  };

  const removeClass = async (label) => {
    if (!window.confirm(`Remove class "${label}"?\nExisting annotations with this label are kept but the class won't appear in the picker.`)) return;
    try {
      const { data } = await api.delete(`/projects/${pid}/classes/${encodeURIComponent(label)}`);
      setProject({ ...project, classes: data.classes });
      toast.success(`Removed class "${label}"`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to remove class");
    }
  };

  const deleteVideo = async (id) => {
    if (!window.confirm("Delete this video? Extracted frames will be kept.")) return;
    try {
      await api.delete(`/videos/${id}`);
      toast.success("Deleted");
      load();
    } catch { toast.error("Delete failed"); }
  };

  const startBatchLabel = async () => {
    const unlabeled = images.filter((i) => !i.annotated).length;
    if (unlabeled === 0) return toast.info("All images are already labeled");
    if (!window.confirm(`Auto-label ${unlabeled} unlabeled image${unlabeled > 1 ? "s" : ""} with Gemini? This may take a few minutes.`)) return;
    try {
      const { data } = await api.post(`/projects/${pid}/batch-auto-label`, { only_unlabeled: true });
      toast.success(`Batch job started: ${data.total} images queued`);
      setBatchJob(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Batch failed");
    }
  };

  const loadALQueue = async () => {
    setAlLoading(true);
    try {
      const { data } = await api.get(`/projects/${pid}/active-learning-queue?limit=30`);
      setAlQueue(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to load queue");
    } finally {
      setAlLoading(false);
    }
  };

  // Poll batch job status
  useEffect(() => {
    if (!batchJob || batchJob.status === "completed") return;
    const iv = setInterval(async () => {
      try {
        const { data } = await api.get(`/batch-jobs/${batchJob.id}`);
        setBatchJob(data);
        if (data.status === "completed") {
          toast.success(`Batch complete · ${data.labeled}/${data.total} labeled`);
          load();
        }
      } catch (err) { console.debug("batch job poll failed", err); }
    }, 2500);
    return () => clearInterval(iv);
    // eslint-disable-next-line
  }, [batchJob]);

  const saveSettings = async () => {
    try {
      const { data } = await api.patch(`/projects/${pid}/settings`, settings);
      setSettings(data.settings);
      toast.success("Settings saved");
      setSettingsOpen(false);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    }
  };

  // gemini_allowed_classes: null = not yet customized (treat as "all classes
  // allowed"); toggling any one class materializes that into a real list.
  const toggleGeminiClass = (label) => {
    const current = settings.gemini_allowed_classes ?? project.classes;
    const next = current.includes(label) ? current.filter((c) => c !== label) : [...current, label];
    setSettings({ ...settings, gemini_allowed_classes: next });
  };

  const upload = async (files) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    let ok = 0, fail = 0;
    for (const f of files) {
      const fd = new FormData();
      fd.append("file", f);
      try {
        await api.post(`/projects/${pid}/images`, fd, { headers: { "Content-Type": "multipart/form-data" } });
        ok++;
      } catch (e) {
        fail++;
      }
    }
    setUploading(false);
    if (ok) toast.success(`${ok} image${ok > 1 ? "s" : ""} uploaded`);
    if (fail) toast.error(`${fail} failed`);
    load();
  };

  const onDrop = (e) => {
    e.preventDefault();
    upload(Array.from(e.dataTransfer.files).filter(f => f.type.startsWith("image/")));
  };

  const deleteImage = async (id, e) => {
    e.stopPropagation();
    if (!window.confirm("Delete this image?")) return;
    try {
      await api.delete(`/images/${id}`);
      toast.success("Deleted");
      load();
    } catch { toast.error("Delete failed"); }
  };

  if (loading || !project) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <div className="max-w-[1600px] mx-auto px-6 py-16 text-muted-foreground text-sm">Loading project...</div>
      </div>
    );
  }

  const progress = project.image_count ? Math.round((project.annotated_count / project.image_count) * 100) : 0;

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[{ label: "Projects", to: "/dashboard" }, { label: project.name }]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        {/* Header */}
        <div className="flex flex-wrap items-end justify-between gap-6 mb-10">
          <div>
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">
              // {project.task_type.replace("_", " ")}
            </div>
            <h1 className="font-heading text-3xl md:text-4xl font-bold tracking-tight mb-2">{project.name}</h1>
            <p className="text-sm text-muted-foreground max-w-2xl">{project.description || "No description"}</p>
          </div>

          <div className="flex items-center gap-3">
            {canManage && members.length > 0 && (
              <Dialog open={assignProjectOpen} onOpenChange={setAssignProjectOpen}>
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                    data-testid="assign-project-btn"
                  >
                    <UserPlus className="w-4 h-4 mr-2" /> Assign Project ({assignments.length})
                  </Button>
                </DialogTrigger>
                <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm max-w-md">
                  <DialogHeader>
                    <DialogTitle className="font-heading tracking-tight">Assign project to members</DialogTitle>
                  </DialogHeader>
                  <div className="space-y-2 py-2 max-h-96 overflow-y-auto">
                    {members.map((m) => {
                      const already = assignments.find((a) => a.user_id === m.user_id);
                      const selected = assignUserIds.has(m.user_id) || !!already;
                      return (
                        <button
                          key={m.user_id}
                          onClick={() => {
                            if (already) return;
                            const next = new Set(assignUserIds);
                            if (next.has(m.user_id)) next.delete(m.user_id); else next.add(m.user_id);
                            setAssignUserIds(next);
                          }}
                          disabled={!!already}
                          className={`w-full text-left flex items-center justify-between p-3 border transition-colors ${
                            selected ? "border-primary bg-[#1C1C1C]" : "border-[#27272A] hover:bg-[#1C1C1C]"
                          } ${already ? "opacity-70" : ""}`}
                          data-testid={`assign-user-${m.user_id}`}
                        >
                          <div>
                            <div className="text-sm">{m.name}</div>
                            <div className="text-[10px] text-muted-foreground uppercase tracking-[0.2em]">{m.role}</div>
                          </div>
                          {already ? (
                            <span className="text-[10px] uppercase tracking-[0.2em] text-primary">assigned</span>
                          ) : selected ? (
                            <CheckCircle2 className="w-4 h-4 text-primary" />
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                  <DialogFooter className="gap-2">
                    {assignments.length > 0 && (
                      <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mr-auto self-center">
                        {assignments.length} currently assigned
                      </div>
                    )}
                    <Button
                      onClick={async () => {
                        const ids = Array.from(assignUserIds);
                        if (ids.length === 0) { setAssignProjectOpen(false); return; }
                        try {
                          const { data } = await api.post(`/projects/${pid}/assign`, { user_ids: ids });
                          toast.success(`Assigned ${data.count} member${data.count !== 1 ? "s" : ""}`);
                          setAssignUserIds(new Set());
                          setAssignProjectOpen(false);
                          load();
                        } catch (e) { toast.error(e.response?.data?.detail || "Assign failed"); }
                      }}
                      className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                      data-testid="assign-project-submit-btn"
                    >
                      Assign {assignUserIds.size > 0 ? `${assignUserIds.size}` : ""}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
            <Button
              variant="outline"
              onClick={() => navigate(`/projects/${pid}/versions`)}
              className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
              data-testid="versions-btn"
            >
              <GitBranch className="w-4 h-4 mr-2" /> Versions
            </Button>
            <Button
              variant="outline"
              onClick={() => navigate(`/projects/${pid}/deploy`)}
              className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
              data-testid="deploy-btn"
            >
              <Rocket className="w-4 h-4 mr-2" /> Deploy
            </Button>
            {canManage && (
              <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                    data-testid="project-settings-btn"
                  >
                    <Settings className="w-4 h-4" />
                  </Button>
                </DialogTrigger>
                <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm">
                  <DialogHeader>
                    <DialogTitle className="font-heading tracking-tight">Auto-Label Settings</DialogTitle>
                  </DialogHeader>
                  <div className="space-y-5 py-2">
                    <div>
                      <div className="flex items-center justify-between mb-2">
                        <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Confidence threshold</Label>
                        <span className="text-sm font-bold text-primary" data-testid="confidence-value">{(settings.confidence_threshold * 100).toFixed(0)}%</span>
                      </div>
                      <input
                        type="range"
                        min="0.05"
                        max="0.95"
                        step="0.05"
                        value={settings.confidence_threshold}
                        onChange={(e) => setSettings({ ...settings, confidence_threshold: parseFloat(e.target.value) })}
                        className="w-full accent-primary"
                        data-testid="confidence-slider"
                      />
                      <div className="text-[10px] text-muted-foreground mt-1">Only keep predictions above this confidence level</div>
                    </div>

                    <div>
                      <div className="flex items-center justify-between mb-2">
                        <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Min boxes before fallback</Label>
                        <span className="text-sm font-bold" data-testid="min-boxes-value">{settings.min_boxes_threshold}</span>
                      </div>
                      <input
                        type="range"
                        min="0"
                        max="10"
                        step="1"
                        value={settings.min_boxes_threshold}
                        onChange={(e) => setSettings({ ...settings, min_boxes_threshold: parseInt(e.target.value, 10) })}
                        className="w-full accent-primary"
                        data-testid="min-boxes-slider"
                      />
                      <div className="text-[10px] text-muted-foreground mt-1">If model finds fewer boxes than this, invoke Gemini fallback</div>
                    </div>

                    <label className="flex items-center gap-3 cursor-pointer" data-testid="fallback-toggle-wrap">
                      <input
                        type="checkbox"
                        checked={settings.fallback_to_gemini}
                        onChange={(e) => setSettings({ ...settings, fallback_to_gemini: e.target.checked })}
                        className="w-4 h-4 accent-primary"
                        data-testid="fallback-toggle"
                      />
                      <div>
                        <div className="text-sm">Fall back to Gemini when model is uncertain</div>
                        <div className="text-[10px] text-muted-foreground">If disabled: keep only model predictions (may be empty)</div>
                      </div>
                    </label>

                    <label className="flex items-center gap-3 cursor-pointer" data-testid="show-confidence-toggle-wrap">
                      <input
                        type="checkbox"
                        checked={settings.show_confidence}
                        onChange={(e) => setSettings({ ...settings, show_confidence: e.target.checked })}
                        className="w-4 h-4 accent-primary"
                        data-testid="show-confidence-toggle"
                      />
                      <div>
                        <div className="text-sm">Show confidence scores on annotations</div>
                        <div className="text-[10px] text-muted-foreground">Display % badge next to each auto-labeled box</div>
                      </div>
                    </label>

                    <label className="flex items-center gap-3 cursor-pointer" data-testid="gemini-restrict-toggle-wrap">
                      <input
                        type="checkbox"
                        checked={settings.gemini_restrict_to_classes}
                        onChange={(e) => setSettings({ ...settings, gemini_restrict_to_classes: e.target.checked })}
                        className="w-4 h-4 accent-primary"
                        data-testid="gemini-restrict-toggle"
                      />
                      <div>
                        <div className="text-sm">Restrict Gemini to this project's existing classes</div>
                        <div className="text-[10px] text-muted-foreground">If disabled: Gemini may invent new labels not yet in your class list</div>
                      </div>
                    </label>

                    {settings.gemini_restrict_to_classes && (
                      <div className="pl-7 border-l border-[#27272A]" data-testid="gemini-allowed-classes-list">
                        <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">
                          Allowed classes ({(settings.gemini_allowed_classes ?? project.classes).length}/{project.classes.length})
                        </div>
                        {project.classes.length === 0 ? (
                          <div className="text-xs text-muted-foreground">No classes yet.</div>
                        ) : (
                          <div className="flex flex-wrap gap-x-4 gap-y-2 max-h-40 overflow-y-auto">
                            {project.classes.map((c) => (
                              <label key={c} className="flex items-center gap-2 cursor-pointer" data-testid={`gemini-class-toggle-wrap-${c}`}>
                                <input
                                  type="checkbox"
                                  checked={(settings.gemini_allowed_classes ?? project.classes).includes(c)}
                                  onChange={() => toggleGeminiClass(c)}
                                  className="w-3.5 h-3.5 accent-primary"
                                  data-testid={`gemini-class-toggle-${c}`}
                                />
                                <span className="text-xs">{c}</span>
                              </label>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={saveSettings}
                      className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                      data-testid="settings-save-btn"
                    >
                      Save Settings
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
          </div>
        </div>

        {/* Training nudge */}
        {project.image_count > 0 && project.annotated_count >= 30 && (
          <div className="mb-6 panel p-4 flex items-center gap-3 border-l-2 border-l-primary" data-testid="train-nudge">
            <Sparkles className="w-5 h-5 text-primary flex-shrink-0" />
            <div className="flex-1">
              <div className="text-sm font-medium">
                You have {project.annotated_count} labeled images — enough to train your own model!
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Train YOLOv8n on your data and start auto-labeling without depending on Gemini.
              </div>
            </div>
            <Button
              size="sm"
              onClick={() => navigate(`/projects/${pid}/deploy`)}
              className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
              data-testid="train-nudge-btn"
            >
              Train Model →
            </Button>
          </div>
        )}

        {/* Stats */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-0 border-l border-t border-[#27272A] mb-10">
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Total Images</div>
            <div className="font-heading text-3xl font-bold" data-testid="stat-total-images">{project.image_count}</div>
          </div>
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Annotated</div>
            <div className="font-heading text-3xl font-bold text-primary" data-testid="stat-annotated">{project.annotated_count}</div>
          </div>
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Classes</div>
            <div className="font-heading text-3xl font-bold" data-testid="stat-classes">{project.classes.length}</div>
          </div>
          <div className="border-r border-b border-[#27272A] p-5">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Progress</div>
            <div className="font-heading text-3xl font-bold">{progress}%</div>
            <div className="mt-2 h-1 bg-[#27272A]">
              <div className="h-full bg-primary transition-all" style={{ width: `${progress}%` }} />
            </div>
          </div>
        </div>

        {/* Classes */}
        <div className="mb-10">
          <div className="flex items-center justify-between mb-3">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
              Class Labels {project.classes.length > 0 && `(${project.classes.length})`}
            </div>
            {canManage && (
              <Dialog open={bulkClassOpen} onOpenChange={setBulkClassOpen}>
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-[10px] uppercase tracking-[0.2em] h-7"
                    data-testid="manage-classes-btn"
                  >
                    Manage Classes
                  </Button>
                </DialogTrigger>
                <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm max-w-lg">
                  <DialogHeader>
                    <DialogTitle className="font-heading tracking-tight">Manage Classes</DialogTitle>
                  </DialogHeader>
                  <div className="space-y-4 py-2">
                    <div>
                      <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">
                        Add multiple classes (comma or new-line separated)
                      </Label>
                      <textarea
                        value={bulkClassText}
                        onChange={(e) => setBulkClassText(e.target.value)}
                        placeholder="car, person, dog&#10;bicycle&#10;truck"
                        rows={4}
                        className="w-full rounded-sm bg-transparent border border-[#27272A] p-3 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-primary focus:border-primary"
                        data-testid="bulk-classes-input"
                      />
                    </div>
                    {project.classes.length > 0 && (
                      <div>
                        <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">
                          Existing classes
                        </Label>
                        <div className="flex flex-wrap gap-2 max-h-40 overflow-y-auto">
                          {project.classes.map((c) => (
                            <span key={c} className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-[#27272A] bg-[#0a0a0a]" data-testid={`class-chip-${c}`}>
                              {c}
                              <button
                                onClick={() => removeClass(c)}
                                className="text-muted-foreground hover:text-destructive"
                                data-testid={`remove-class-${c}`}
                                title="Remove class"
                              >
                                <XCircle className="w-3 h-3" />
                              </button>
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={addBulkClasses}
                      disabled={!bulkClassText.trim()}
                      className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                      data-testid="bulk-classes-submit"
                    >
                      Add Classes
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
          </div>
          {project.classes.length === 0 ? (
            <div className="text-xs text-muted-foreground">No classes yet. {canManage && "Click Manage Classes to add multiple at once."}</div>
          ) : (
            <div className="flex flex-wrap gap-2">
              {project.classes.map((c) => (
                <span key={c} className="text-xs px-3 py-1 border border-[#27272A] bg-[#121212]" data-testid={`class-tag-${c}`}>
                  {c}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Upload zone */}
        <Tabs defaultValue="images" className="space-y-6">
          <TabsList className="bg-transparent border border-[#27272A] rounded-sm p-0 h-auto">
            <TabsTrigger value="images" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-images">
              Images ({images.length})
            </TabsTrigger>
            <TabsTrigger value="upload" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-upload">
              Upload
            </TabsTrigger>
            <TabsTrigger value="video" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-video">
              Video ({videos.length})
            </TabsTrigger>
            <TabsTrigger value="queue" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-queue" onClick={() => !alQueue && loadALQueue()}>
              <TrendingUp className="w-3 h-3 mr-2" /> AL Queue
            </TabsTrigger>
          </TabsList>

          <TabsContent value="upload">
            <div
              onDragOver={(e) => e.preventDefault()}
              onDrop={onDrop}
              onClick={() => fileInputRef.current?.click()}
              className="border-2 border-dashed border-[#27272A] hover:border-primary p-16 text-center cursor-pointer transition-colors"
              data-testid="upload-dropzone"
            >
              <Upload className="w-10 h-10 text-primary mx-auto mb-6" strokeWidth={1.5} />
              <h3 className="font-heading text-xl mb-2">Drop images here</h3>
              <p className="text-sm text-muted-foreground mb-4">or click to browse · jpg, png, webp · max 10MB each</p>
              <Button
                disabled={uploading}
                className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="upload-select-btn"
              >
                {uploading ? "Uploading..." : "Select Files"}
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => upload(Array.from(e.target.files))}
                data-testid="upload-file-input"
              />
            </div>
          </TabsContent>

          <TabsContent value="images">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
              <div className="flex items-center gap-2 border border-[#27272A] p-1 rounded-sm" data-testid="status-filter">
                {[
                  { id: "all", label: "All" },
                  { id: "unassigned", label: "Unassigned" },
                  { id: "assigned", label: "Assigned" },
                  { id: "submitted", label: "In Review" },
                  { id: "approved", label: "Approved" },
                  { id: "rejected", label: "Rejected" },
                ].map((f) => (
                  <button
                    key={f.id}
                    onClick={() => setStatusFilter(f.id)}
                    className={`px-3 py-1 text-[10px] uppercase tracking-[0.2em] transition-colors ${
                      statusFilter === f.id ? "bg-[#1C1C1C] text-primary" : "text-muted-foreground hover:text-foreground"
                    }`}
                    data-testid={`filter-${f.id}`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>

              <div className="flex items-center gap-2">
                {canManage && selectedIds.size > 0 && (
                  <Dialog open={bulkOpen} onOpenChange={setBulkOpen}>
                    <DialogTrigger asChild>
                      <Button variant="outline" className="rounded-sm border-primary text-primary bg-[#1C1C1C] hover:bg-[#252525] text-xs uppercase tracking-[0.2em]" data-testid="bulk-assign-btn">
                        <UserPlus className="w-4 h-4 mr-2" /> Bulk Assign ({selectedIds.size})
                      </Button>
                    </DialogTrigger>                    <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm">
                      <DialogHeader>
                        <DialogTitle className="font-heading tracking-tight">Assign {selectedIds.size} images</DialogTitle>
                      </DialogHeader>
                      <div className="space-y-4 py-2">
                        <div>
                          <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Assignee</Label>
                          <Select value={bulkUser} onValueChange={setBulkUser}>
                            <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="bulk-user-select">
                              <SelectValue placeholder="Choose member" />
                            </SelectTrigger>
                            <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                              {members.map((m) => (
                                <SelectItem key={m.user_id} value={m.user_id}>{m.name} ({m.role})</SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        <div>
                          <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Priority</Label>
                          <Select value={bulkPriority} onValueChange={setBulkPriority}>
                            <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="bulk-priority-select">
                              <SelectValue placeholder="(keep current)" />
                            </SelectTrigger>
                            <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                              <SelectItem value="low">Low</SelectItem>
                              <SelectItem value="medium">Medium</SelectItem>
                              <SelectItem value="high">High</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                        <div>
                          <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Task notes</Label>
                          <Input value={bulkNotes} onChange={(e) => setBulkNotes(e.target.value)} placeholder="Optional instructions" className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary" data-testid="bulk-notes-input" />
                        </div>
                      </div>
                      <DialogFooter>
                        <Button
                          onClick={async () => {
                            if (!bulkUser) { toast.error("Pick a member"); return; }
                            try {
                              const { data } = await api.post(`/projects/${pid}/images/bulk-assign`, {
                                image_ids: Array.from(selectedIds),
                                user_id: bulkUser,
                                priority: bulkPriority || undefined,
                                notes: bulkNotes || undefined,
                              });
                              toast.success(`Assigned ${data.updated} image${data.updated !== 1 ? "s" : ""}`);
                              setSelectedIds(new Set());
                              setBulkOpen(false);
                              setBulkUser(""); setBulkPriority(""); setBulkNotes("");
                              load();
                            } catch (e) { toast.error(e.response?.data?.detail || "Assign failed"); }
                          }}
                          className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
                          data-testid="bulk-submit-btn"
                        >
                          Assign
                        </Button>
                      </DialogFooter>
                    </DialogContent>
                  </Dialog>
                )}
                {canManage && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      const filtered = images.filter((img) => statusFilter === "all" || (img.review_status || "unassigned") === statusFilter);
                      const filteredIds = filtered.map((i) => i.id);
                      const allSelected = filteredIds.every((id) => selectedIds.has(id));
                      if (allSelected) {
                        // Deselect all filtered
                        const next = new Set(selectedIds);
                        filteredIds.forEach((id) => next.delete(id));
                        setSelectedIds(next);
                      } else {
                        // Select all filtered (union)
                        const next = new Set(selectedIds);
                        filteredIds.forEach((id) => next.add(id));
                        setSelectedIds(next);
                      }
                    }}
                    className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em] h-8"
                    data-testid="select-all-btn"
                  >
                    {(() => {
                      const filtered = images.filter((img) => statusFilter === "all" || (img.review_status || "unassigned") === statusFilter);
                      const filteredIds = filtered.map((i) => i.id);
                      const allSelected = filteredIds.length > 0 && filteredIds.every((id) => selectedIds.has(id));
                      return allSelected ? `Deselect All (${filteredIds.length})` : `Select All (${filteredIds.length})`;
                    })()}
                  </Button>
                )}
                {selectedIds.size > 0 && (
                  <Button variant="ghost" size="sm" onClick={() => setSelectedIds(new Set())} className="rounded-sm h-8 text-xs uppercase tracking-[0.2em]" data-testid="clear-selection-btn">
                    Clear
                  </Button>
                )}
                <Button
                  variant="outline"
                  onClick={() => navigate(`/projects/${pid}/activity`)}
                  className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                  data-testid="activity-btn"
                >
                  <ActivityIcon className="w-4 h-4 mr-2" /> Activity
                </Button>
                <Button
                  onClick={startBatchLabel}
                  disabled={batchJob && batchJob.status !== "completed"}
                  variant="outline"
                  className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                  data-testid="batch-label-btn"
                >
                  <Sparkles className="w-4 h-4 mr-2" />
                  {batchJob && batchJob.status !== "completed"
                    ? `Labeling ${batchJob.processed}/${batchJob.total}...`
                    : "Batch Auto-Label"}
                </Button>
              </div>
            </div>

            {batchJob && batchJob.status !== "completed" && (
              <div className="mb-4 panel p-3">
                <div className="flex items-center justify-between mb-2 text-xs">
                  <span className="text-primary uppercase tracking-[0.2em]">Batch job · {batchJob.status}</span>
                  <span className="text-muted-foreground">
                    {batchJob.processed}/{batchJob.total} · {batchJob.labeled} labeled · {batchJob.failed} failed
                    {batchJob.source_counts && (
                      <span className="ml-2 text-primary" data-testid="batch-source-counts">
                        · model {batchJob.source_counts.model || 0} / gemini {batchJob.source_counts.gemini || 0}
                      </span>
                    )}
                  </span>
                </div>
                <div className="h-1 bg-[#27272A]">
                  <div
                    className="h-full bg-primary transition-all"
                    style={{ width: `${Math.round((batchJob.processed / batchJob.total) * 100)}%` }}
                    data-testid="batch-progress"
                  />
                </div>
              </div>
            )}

            {(() => {
              const filtered = images.filter((img) => statusFilter === "all" || (img.review_status || "unassigned") === statusFilter);
              if (filtered.length === 0) {
                return (
                  <div className="border border-dashed border-[#27272A] p-16 text-center">
                    <ImageIcon className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
                    <h3 className="font-heading text-xl mb-2">
                      {statusFilter === "all" ? "No images yet" : `No ${statusFilter} images`}
                    </h3>
                    <p className="text-sm text-muted-foreground mb-6">
                      {statusFilter === "all" ? "Upload images or extract frames from a video." : "Try a different filter or start annotating."}
                    </p>
                  </div>
                );
              }
              return (
                <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
                  {filtered.map((img) => {
                    const rs = img.review_status || "unassigned";
                    const badgeMeta = rs === "approved" ? { color: "bg-[#22C55E] text-black", icon: CheckCircle2 } :
                                      rs === "rejected" ? { color: "bg-destructive text-white", icon: XCircle } :
                                      rs === "submitted" ? { color: "bg-[#EAB308] text-black", icon: Send } :
                                      rs === "assigned" ? { color: "bg-primary text-black", icon: Clock } :
                                      null;
                    const Icon = badgeMeta?.icon;
                    return (
                      <div
                        key={img.id}
                        onClick={(e) => {
                          if (e.shiftKey || canManage && selectedIds.size > 0) {
                            const next = new Set(selectedIds);
                            if (next.has(img.id)) next.delete(img.id); else next.add(img.id);
                            setSelectedIds(next);
                          } else {
                            navigate(`/projects/${pid}/annotate/${img.id}`);
                          }
                        }}
                        className={`relative group cursor-pointer border overflow-hidden bg-[#0a0a0a] transition-colors ${
                          selectedIds.has(img.id) ? "border-primary" : "border-[#27272A] hover:border-primary"
                        }`}
                        data-testid={`image-card-${img.id}`}
                      >
                        {canManage && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              const next = new Set(selectedIds);
                              if (next.has(img.id)) next.delete(img.id); else next.add(img.id);
                              setSelectedIds(next);
                            }}
                            className={`absolute top-2 right-2 z-10 w-5 h-5 border transition-colors flex items-center justify-center ${
                              selectedIds.has(img.id) ? "bg-primary border-primary" : "bg-[#050505]/80 border-[#27272A] opacity-0 group-hover:opacity-100"
                            }`}
                            data-testid={`select-image-${img.id}`}
                          >
                            {selectedIds.has(img.id) && <CheckCircle2 className="w-3 h-3 text-black" />}
                          </button>
                        )}
                        <div className="aspect-square relative">
                          <img
                            src={fileUrl(img.storage_path)}
                            alt={img.filename}
                            className="w-full h-full object-cover"
                            draggable={false}
                            loading="lazy"
                          />
                          {img.annotated && (
                            <div className="absolute top-2 right-2 bg-primary text-black w-6 h-6 flex items-center justify-center" title="Annotated">
                              <CheckCircle2 className="w-4 h-4" />
                            </div>
                          )}
                          {badgeMeta && (
                            <div className={`absolute bottom-2 left-2 px-1.5 py-0.5 text-[9px] uppercase tracking-wider flex items-center gap-1 ${badgeMeta.color}`} title={`Status: ${rs}`} data-testid={`status-badge-${img.id}`}>
                              <Icon className="w-2.5 h-2.5" /> {rs === "submitted" ? "review" : rs}
                            </div>
                          )}
                          {img.from_video && (
                            <div className="absolute bottom-2 right-2 bg-[#050505]/80 text-primary px-1.5 py-0.5 text-[9px] uppercase tracking-wider flex items-center gap-1" title="From video">
                              <Film className="w-2.5 h-2.5" /> Video
                            </div>
                          )}
                          <button
                            onClick={(e) => deleteImage(img.id, e)}
                            className="absolute top-2 left-2 opacity-0 group-hover:opacity-100 bg-[#050505]/80 text-muted-foreground hover:text-destructive w-6 h-6 flex items-center justify-center transition-opacity"
                            data-testid={`delete-image-${img.id}`}
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </div>
                        <div className="p-2 border-t border-[#27272A]">
                          <div className="text-[10px] truncate text-muted-foreground">{img.filename}</div>
                          <div className="text-[10px] text-primary mt-1">
                            {img.annotations?.length || 0} annotation{(img.annotations?.length || 0) !== 1 ? "s" : ""}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              );
            })()}
          </TabsContent>

          <TabsContent value="video">
            <div className="space-y-6">
              <div className="panel p-6">
                <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// Upload video</div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
                  <div>
                    <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Extract Mode</Label>
                    <Select value={videoIntervalMode} onValueChange={setVideoIntervalMode}>
                      <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="video-interval-mode">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                        <SelectItem value="frames">Every N frames</SelectItem>
                        <SelectItem value="seconds">Every N seconds</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">
                      {videoIntervalMode === "seconds" ? "Interval (seconds)" : "Interval (frames)"}
                    </Label>
                    <Input
                      type="number"
                      min={videoIntervalMode === "seconds" ? "0.1" : "1"}
                      step={videoIntervalMode === "seconds" ? "0.5" : "1"}
                      value={videoInterval}
                      onChange={(e) => setVideoInterval(Number(e.target.value) || 1)}
                      className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                      data-testid="video-interval-input"
                    />
                    <div className="text-[10px] text-muted-foreground mt-1">
                      {videoIntervalMode === "seconds"
                        ? `Grab 1 frame every ${videoInterval}s (adapts to video FPS)`
                        : `Grab 1 frame every ${videoInterval} frames`}
                    </div>
                  </div>
                  <div>
                    <Button
                      onClick={() => videoInputRef.current?.click()}
                      disabled={uploading}
                      className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 text-xs uppercase tracking-[0.2em] font-bold w-full"
                      data-testid="video-upload-btn"
                    >
                      <Video className="w-4 h-4 mr-2" /> {uploading ? "Uploading..." : "Select Video"}
                    </Button>
                    <input
                      ref={videoInputRef}
                      type="file"
                      accept="video/mp4,video/quicktime,video/webm,video/x-msvideo"
                      className="hidden"
                      onChange={(e) => uploadVideo(e.target.files?.[0])}
                      data-testid="video-file-input"
                    />
                  </div>
                </div>
              </div>

              {videos.length === 0 ? (
                <div className="border border-dashed border-[#27272A] p-16 text-center">
                  <Film className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
                  <h3 className="font-heading text-xl mb-2">No videos uploaded</h3>
                  <p className="text-sm text-muted-foreground">Upload a video and frames will be extracted automatically.</p>
                </div>
              ) : (
                <div className="border-t border-[#27272A]">
                  {videos.map((v) => {
                    const isProcessing = v.status === "processing" || v.status === "queued";
                    const pct = v.total_frames > 0 ? Math.min(100, Math.round((v.processed_frames / v.total_frames) * 100)) : (v.status === "completed" ? 100 : 0);
                    return (
                      <div key={v.id} className="border-b border-[#27272A] p-4 flex flex-wrap items-center justify-between gap-4" data-testid={`video-row-${v.id}`}>
                        <div className="flex items-center gap-4 flex-1 min-w-0">
                          <div className="w-9 h-9 bg-[#1C1C1C] border border-[#27272A] flex items-center justify-center">
                            {isProcessing ? <Loader2 className="w-4 h-4 text-primary animate-spin" /> : <Film className="w-4 h-4 text-primary" />}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="font-heading font-medium text-sm truncate">{v.filename}</div>
                            <div className="text-xs text-muted-foreground">
                              {v.extracted_count} frame{v.extracted_count !== 1 ? "s" : ""} extracted · every {v.interval_frames} frames · {v.status}
                            </div>
                            {isProcessing && (
                              <div className="mt-2 h-1 bg-[#27272A] max-w-md">
                                <div className="h-full bg-primary transition-all" style={{ width: `${pct}%` }} data-testid={`video-progress-${v.id}`} />
                              </div>
                            )}
                          </div>
                        </div>
                        <button
                          onClick={() => deleteVideo(v.id)}
                          className="text-muted-foreground hover:text-destructive transition-colors"
                          data-testid={`delete-video-${v.id}`}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </TabsContent>

          <TabsContent value="queue">
            <div className="panel p-6" data-testid="al-queue-panel">
              <div className="flex items-start justify-between mb-4">
                <div>
                  <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-1">// Active learning</div>
                  <h3 className="font-heading text-lg mb-1">Most Uncertain Images First</h3>
                  <p className="text-xs text-muted-foreground">
                    {alQueue?.has_active_model
                      ? "Ranked by model uncertainty — labeling these first gives the biggest accuracy gain."
                      : "No active trained model. Queue prioritizes unlabeled images. Train a model to enable uncertainty scoring."}
                  </p>
                </div>
                <Button
                  onClick={loadALQueue}
                  variant="outline"
                  size="sm"
                  disabled={alLoading}
                  className="rounded-sm border-[#27272A] text-xs uppercase tracking-[0.2em]"
                  data-testid="al-queue-refresh-btn"
                >
                  {alLoading ? <Loader2 className="w-3 h-3 animate-spin mr-2" /> : <TrendingUp className="w-3 h-3 mr-2" />}
                  Refresh
                </Button>
              </div>

              {!alQueue && !alLoading && (
                <div className="text-sm text-muted-foreground py-8 text-center">Click Refresh to load queue</div>
              )}

              {alQueue && alQueue.items.length === 0 && (
                <div className="text-sm text-muted-foreground py-8 text-center">No images available for labeling</div>
              )}

              {alQueue && alQueue.items.length > 0 && (
                <div className="space-y-2" data-testid="al-queue-list">
                  <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">
                    {alQueue.total_candidates} candidates · showing top {alQueue.items.length}
                  </div>
                  {alQueue.items.map((it, i) => (
                    <div
                      key={it.image_id}
                      onClick={() => navigate(`/projects/${pid}/annotate/${it.image_id}`)}
                      className="flex items-center gap-4 p-3 border border-[#27272A] hover:border-primary bg-[#0a0a0a] cursor-pointer transition-colors"
                      data-testid={`al-queue-item-${i}`}
                    >
                      <div className="text-primary font-mono text-xs w-6">{i + 1}</div>
                      <img
                        src={fileUrl(it.storage_path)}
                        alt={it.filename}
                        className="w-16 h-16 object-cover border border-[#27272A]"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-mono truncate">{it.filename}</div>
                        <div className="text-[10px] text-muted-foreground uppercase tracking-[0.2em] mt-1">
                          {it.review_status} · {it.annotation_count} annotations · {it.reason.replace(/_/g, " ")}
                        </div>
                      </div>
                      <div className="flex-shrink-0 w-24">
                        <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-1">Uncertainty</div>
                        <div className="flex items-center gap-2">
                          <div className="flex-1 h-1.5 bg-[#1C1C1C]">
                            <div
                              className="h-full bg-primary"
                              style={{ width: `${it.uncertainty * 100}%` }}
                            />
                          </div>
                          <div className="text-xs font-mono text-primary" data-testid={`al-uncertainty-${i}`}>
                            {Math.round(it.uncertainty * 100)}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
