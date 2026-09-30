import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { Rocket, Cpu, TrendingUp, Copy, Play, Zap, CheckCircle2, Loader2, AlertCircle, Sparkles, Pencil, Check, X } from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import DeployPipeline from "@/pages/DeployPipeline";
import { Server } from "lucide-react";

// Locally trained ("real") and server-trained (deploy pipeline) models share one list.
const isTrainedModel = (m) => m.type === "real" || m.type === "pipeline_candidate";

export default function ModelDeploy() {
  const { pid } = useParams();
  const [params] = useSearchParams();
  const [project, setProject] = useState(null);
  const [versions, setVersions] = useState([]);
  const [models, setModels] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState(params.get("version") || "");
  const [modelArch, setModelArch] = useState("yolov8n");
  const [epochs, setEpochs] = useState(30);
  const [training, setTraining] = useState(false);
  const [selectedModel, setSelectedModel] = useState(null);
  const [realTraining, setRealTraining] = useState(null);
  const [realEpochs, setRealEpochs] = useState(0); // 0 = auto
  const [realArch, setRealArch] = useState("yolov8n");
  const [trainingPlan, setTrainingPlan] = useState(null);
  const [sysVersions, setSysVersions] = useState(null);
  const [activating, setActivating] = useState(null);
  const [switchingToGemini, setSwitchingToGemini] = useState(false);
  const [editingModelId, setEditingModelId] = useState(null);
  const [editModelName, setEditModelName] = useState("");

  const load = async () => {
    try {
      const [p, v, m, tp, sv] = await Promise.all([
        api.get(`/projects/${pid}`),
        api.get(`/projects/${pid}/versions`),
        api.get(`/projects/${pid}/models`),
        api.get(`/projects/${pid}/training-plan`).catch(() => ({ data: null })),
        api.get(`/system/versions`).catch(() => ({ data: null })),
      ]);
      setProject(p.data);
      setVersions(v.data);
      setModels(m.data);
      setTrainingPlan(tp.data);
      setSysVersions(sv.data);
      if (!selectedVersion && v.data.length > 0) setSelectedVersion(v.data[0].id);
      const simulated = m.data.filter((x) => !isTrainedModel(x));
      if (simulated.length > 0) setSelectedModel(simulated[0]);
    } catch { toast.error("Failed to load"); }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [pid]);

  // Poll active training job
  useEffect(() => {
    if (!realTraining) return;
    if (["trained", "failed", "cancelled"].includes(realTraining.status)) return;
    const iv = setInterval(async () => {
      try {
        const { data } = await api.get(`/models/${realTraining.id}`);
        setRealTraining(data);
        if (data.status === "trained") {
          toast.success(`Model trained · mAP@50 ${(data.final_mAP * 100).toFixed(1)}%`);
          load();
        } else if (data.status === "failed") {
          toast.error(`Training failed: ${data.error || "unknown"}`);
        } else if (data.status === "cancelled") {
          toast.info("Training cancelled");
          load();
        }
      } catch (err) { console.debug("training poll failed", err); }
    }, 5000);
    return () => clearInterval(iv);
  }, [realTraining]);

  const trainReal = async () => {
    try {
      const { data } = await api.post(`/projects/${pid}/train-real?epochs=${realEpochs}&model_arch=${realArch}`);
      const modeMsg = data.epochs_auto_scaled ? `auto-scaled to ${data.epochs}` : `${data.epochs}`;
      toast.success(`Real training started · ${realArch} · ${modeMsg} epochs on ${data.training_image_count} images`);
      setRealTraining(data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Training failed to start");
    }
  };

  const activateModel = async (mid) => {
    setActivating(mid);
    try {
      await api.post(`/models/${mid}/activate`);
      toast.success("Model activated — will now auto-label new images");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Activation failed");
    } finally {
      setActivating(null);
    }
  };

  const useGemini = async () => {
    setSwitchingToGemini(true);
    try {
      await api.post(`/projects/${pid}/models/use-gemini`);
      toast.success("Switched auto-label back to Gemini");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to switch to Gemini");
    } finally {
      setSwitchingToGemini(false);
    }
  };

  const cancelTraining = async (mid) => {
    if (!window.confirm("Cancel this training run? Progress will be lost.")) return;
    try {
      await api.post(`/models/${mid}/cancel`);
      toast.success("Cancellation requested — will stop after current epoch");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Cancel failed");
    }
  };

  const startRenaming = (m) => {
    setEditingModelId(m.id);
    setEditModelName(m.name || "");
  };

  const saveRename = (mid) => {
    const name = editModelName.trim();
    setEditingModelId(null);
    // Optimistic + background, same reasoning as class deletion elsewhere -
    // this backend's per-request latency makes a blocking await feel stuck.
    setModels((prev) => prev.map((m) => (m.id === mid ? { ...m, name: name || null } : m)));
    api.patch(`/models/${mid}`, { name })
      .catch((e) => {
        toast.error(e.response?.data?.detail || "Rename failed");
        load();
      });
  };

  const deleteModel = async (mid) => {
    if (!window.confirm("Delete this model? This cannot be undone.")) return;
    try {
      await api.delete(`/models/${mid}`);
      toast.success("Model deleted");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    }
  };

  const train = async () => {
    if (!selectedVersion) return toast.error("Select a version first");
    setTraining(true);
    try {
      const { data } = await api.post(`/projects/${pid}/train`, {
        version_id: selectedVersion,
        epochs: Number(epochs),
        model_arch: modelArch,
      });
      toast.success(`Model trained · mAP ${(data.final_mAP * 100).toFixed(1)}%`);
      setModels([data, ...models]);
      setSelectedModel(data);
    } catch (e) {
      toast.error("Training failed");
    } finally {
      setTraining(false);
    }
  };

  const copyEndpoint = () => {
    if (!selectedModel) return;
    const url = `${process.env.REACT_APP_BACKEND_URL}${selectedModel.endpoint}`;
    navigator.clipboard.writeText(url);
    toast.success("Endpoint copied");
  };

  if (!project) return <div className="min-h-screen bg-background"><Navbar /></div>;

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[
        { label: "Projects", to: "/dashboard" },
        { label: project.name, to: `/projects/${pid}` },
        { label: "Deploy" },
      ]} />

      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="mb-10">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Model deployment</div>
          <h1 className="font-heading text-4xl font-bold tracking-tight mb-2">Train & Deploy</h1>
          <p className="text-sm text-muted-foreground">
            Train real YOLOv8n models on your approved annotations. Once activated, the model auto-labels new images — replacing Gemini calls.
          </p>
        </div>

        <div className="panel p-5 mb-8 flex flex-wrap items-center justify-between gap-4" data-testid="autolabel-provider-row">
          <div className="flex items-center gap-4">
            <Sparkles className="w-4 h-4 text-primary" />
            <div>
              <div className="font-heading font-medium text-sm flex items-center gap-2">
                Gemini (fallback AI)
                {!models.some((m) => m.is_active && m.status === "trained") && (
                  <span className="text-[9px] uppercase tracking-[0.2em] px-2 py-0.5 bg-primary text-black font-bold" data-testid="gemini-active-badge">Active</span>
                )}
              </div>
              <div className="text-xs text-muted-foreground">Used for auto-labeling whenever no trained model is active.</div>
            </div>
          </div>
          {models.some((m) => m.is_active && m.status === "trained") && (
            <Button
              size="sm"
              onClick={useGemini}
              disabled={switchingToGemini}
              variant="outline"
              className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
              data-testid="use-gemini-btn"
            >
              Use Gemini
            </Button>
          )}
        </div>

        <Tabs defaultValue={["real", "simulated", "pipeline"].includes(params.get("tab")) ? params.get("tab") : "real"} className="space-y-6">
          <TabsList className="bg-transparent border border-[#27272A] rounded-sm p-0 h-auto">
            <TabsTrigger value="real" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-real-training">
              <Zap className="w-3 h-3 mr-2" /> Model Training
            </TabsTrigger>
            <TabsTrigger value="simulated" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-simulated">
              Simulated
            </TabsTrigger>
            <TabsTrigger value="pipeline" className="rounded-sm data-[state=active]:bg-[#1C1C1C] data-[state=active]:text-primary text-xs uppercase tracking-[0.2em] px-4 py-2" data-testid="tab-pipeline">
              <Server className="w-3 h-3 mr-2" /> Deploy Pipeline
            </TabsTrigger>
          </TabsList>

          {/* REAL TRAINING TAB */}
          <TabsContent value="real" className="space-y-8">
            <div className="panel p-6">
              <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">
                // YOLOv8 · trained on your approved annotations
                {sysVersions?.ultralytics && (
                  <span className="text-muted-foreground normal-case ml-2 tracking-normal" data-testid="ultralytics-version">
                    · ultralytics v{sysVersions.ultralytics}
                    {sysVersions.torch && ` · torch ${sysVersions.torch}`}
                    {sysVersions.cuda_available ? " · GPU" : " · CPU"}
                  </span>
                )}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-4 gap-4 items-end">
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Architecture</Label>
                  <Select value={realArch} onValueChange={setRealArch}>
                    <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="real-arch-select">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                      <SelectItem value="yolov8n">YOLOv8n · nano (fast, CPU)</SelectItem>
                      <SelectItem value="yolov8s">YOLOv8s · small</SelectItem>
                      <SelectItem value="yolov8m">YOLOv8m · medium</SelectItem>
                      <SelectItem value="yolov8l">YOLOv8l · large (accurate, slow)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Epochs (0 = auto-scale)</Label>
                  <Input
                    type="number"
                    min="0"
                    max="200"
                    value={realEpochs}
                    onChange={(e) => setRealEpochs(Math.max(0, Number(e.target.value) || 0))}
                    className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                    data-testid="real-epochs-input"
                  />
                </div>
                <div className="md:col-span-2">
                  <Button
                    onClick={trainReal}
                    disabled={realTraining && !["trained", "failed", "cancelled"].includes(realTraining.status)}
                    className="w-full rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 text-xs uppercase tracking-[0.2em] font-bold"
                    data-testid="train-real-btn"
                  >
                    <Play className="w-4 h-4 mr-2" />
                    {realTraining && !["trained", "failed", "cancelled"].includes(realTraining.status)
                      ? `${realTraining.status}...`
                      : `Train ${realArch}`}
                  </Button>
                </div>
              </div>
              <div className="mt-3 text-[10px] text-muted-foreground" data-testid="training-plan-hint">
                Requires ≥ 30 approved/annotated images with bounding boxes.
                {trainingPlan && trainingPlan.ready && (
                  <> Plan: <span className="text-primary">{trainingPlan.eligible_images} images · {realEpochs === 0 ? trainingPlan.recommended_epochs : realEpochs} epochs</span> · est. ~{Math.max(1, Math.round((trainingPlan.eligible_images * (realEpochs === 0 ? trainingPlan.recommended_epochs : realEpochs) * 2) / 60))} min on CPU</>
                )}
                {trainingPlan && !trainingPlan.ready && (
                  <> Currently {trainingPlan.eligible_images}/{trainingPlan.min_required} eligible.</>
                )}
              </div>
              {realTraining && (
                <div className="mt-4 p-3 bg-[#050505] border border-[#27272A] flex items-center gap-3" data-testid="training-progress">
                  {realTraining.status === "trained" ? (
                    <CheckCircle2 className="w-5 h-5 text-[#22C55E]" />
                  ) : realTraining.status === "failed" ? (
                    <AlertCircle className="w-5 h-5 text-destructive" />
                  ) : (
                    <Loader2 className="w-5 h-5 text-primary animate-spin" />
                  )}
                  <div className="flex-1">
                    <div className="text-xs uppercase tracking-[0.2em] text-primary">{realTraining.status}</div>
                    <div className="text-[10px] text-muted-foreground">
                      {realTraining.status === "trained"
                        ? `mAP@50: ${(realTraining.final_mAP * 100).toFixed(1)}% · Precision: ${(realTraining.precision * 100).toFixed(1)}% · Recall: ${(realTraining.recall * 100).toFixed(1)}%`
                        : realTraining.status === "failed"
                          ? realTraining.error
                          : realTraining.status === "cancelled"
                            ? "Training cancelled by user"
                            : `Training on ${realTraining.training_image_count} images · epoch ${realTraining.current_epoch || 0}/${realTraining.epochs}${realTraining.progress != null ? ` · ${realTraining.progress}%` : ""}`}
                    </div>
                  </div>
                  {realTraining.status === "trained" && !realTraining.is_active && (
                    <Button
                      size="sm"
                      onClick={() => activateModel(realTraining.id)}
                      disabled={activating === realTraining.id}
                      className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-8 text-xs uppercase tracking-[0.2em] font-bold"
                      data-testid="activate-realtraining-btn"
                    >
                      Set Active
                    </Button>
                  )}
                  {["queued", "preparing", "training"].includes(realTraining.status) && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => cancelTraining(realTraining.id)}
                      className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 h-8 text-xs uppercase tracking-[0.2em]"
                      data-testid="cancel-training-btn"
                    >
                      Cancel
                    </Button>
                  )}
                </div>
              )}
            </div>

            {/* Real model runs list */}
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">
                Trained Models ({models.filter(isTrainedModel).length})
              </div>
              {models.filter(isTrainedModel).length === 0 ? (
                <div className="border border-dashed border-[#27272A] p-16 text-center">
                  <Zap className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
                  <h3 className="font-heading text-xl mb-2">No trained models yet</h3>
                  <p className="text-sm text-muted-foreground">Train your first YOLOv8n on approved annotations, or train on a server from the Deploy Pipeline tab.</p>
                </div>
              ) : (
                <div className="border-t border-[#27272A]">
                  {models.filter(isTrainedModel).map((m) => (
                    <div key={m.id} className="border-b border-[#27272A] p-5 flex flex-wrap items-center justify-between gap-4 panel-hover" data-testid={`real-model-${m.id}`}>
                      <div className="flex items-center gap-4">
                        <Cpu className="w-4 h-4 text-primary" />
                        <div>
                          <div className="font-heading font-medium text-sm flex items-center gap-2">
                            {editingModelId === m.id ? (
                              <>
                                <input
                                  autoFocus
                                  value={editModelName}
                                  onChange={(e) => setEditModelName(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") saveRename(m.id);
                                    if (e.key === "Escape") setEditingModelId(null);
                                  }}
                                  placeholder={`${m.model_arch} · ${m.epochs ?? "?"} epochs`}
                                  className="bg-transparent border border-[#27272A] focus:outline-none focus:border-primary rounded-sm px-2 py-0.5 text-sm font-heading"
                                  data-testid={`model-name-input-${m.id}`}
                                />
                                <button onClick={() => saveRename(m.id)} className="text-primary hover:text-cyan-400" data-testid={`model-name-save-${m.id}`}>
                                  <Check className="w-3.5 h-3.5" />
                                </button>
                                <button onClick={() => setEditingModelId(null)} className="text-muted-foreground hover:text-destructive" data-testid={`model-name-cancel-${m.id}`}>
                                  <X className="w-3.5 h-3.5" />
                                </button>
                              </>
                            ) : (
                              <>
                                <span data-testid={`model-name-${m.id}`}>{m.name || `${m.model_arch} · ${m.epochs ?? "?"} epochs`}</span>
                                <button onClick={() => startRenaming(m)} className="text-muted-foreground hover:text-primary" title="Rename" data-testid={`model-name-edit-${m.id}`}>
                                  <Pencil className="w-3 h-3" />
                                </button>
                              </>
                            )}
                            {m.is_active && (
                              <span className="text-[9px] uppercase tracking-[0.2em] px-2 py-0.5 bg-primary text-black font-bold" data-testid={`active-badge-${m.id}`}>Active</span>
                            )}
                            {m.source === "server" && (
                              <span className="text-[9px] uppercase tracking-[0.2em] px-2 py-0.5 border border-primary text-primary font-bold" data-testid={`server-badge-${m.id}`}>Trained from server</span>
                            )}
                          </div>
                          <div className="text-xs text-muted-foreground">
                            {m.training_image_count ?? "?"} images
                            {Array.isArray(m.classes) && ` · ${m.classes.length} classes`}
                            {m.trained_on && ` · Trained on ${m.trained_on === "server" ? "server" : m.trained_on === "cuda" ? "GPU" : "CPU"}`}
                            {` · ${new Date(m.created_at).toLocaleString()} · ${m.status}`}
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-6 text-xs">
                        {m.status === "trained" && (
                          <>
                            <span className="text-primary font-bold">mAP@50 {((m.final_mAP || 0) * 100).toFixed(1)}%</span>
                            <span className="text-muted-foreground">P {((m.precision || 0) * 100).toFixed(1)}%</span>
                            <span className="text-muted-foreground">R {((m.recall || 0) * 100).toFixed(1)}%</span>
                            {!m.is_active && (
                              <Button
                                size="sm"
                                onClick={() => activateModel(m.id)}
                                disabled={activating === m.id}
                                variant="outline"
                                className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
                                data-testid={`activate-btn-${m.id}`}
                              >
                                Set Active
                              </Button>
                            )}
                          </>
                        )}
                        {m.status === "training" && (
                          <>
                            <Loader2 className="w-4 h-4 text-primary animate-spin" />
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => cancelTraining(m.id)}
                              className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 text-xs uppercase tracking-[0.2em]"
                              data-testid={`cancel-btn-${m.id}`}
                            >
                              Cancel
                            </Button>
                          </>
                        )}
                        {m.status === "failed" && <AlertCircle className="w-4 h-4 text-destructive" title={m.error} />}
                        {!["queued", "preparing", "training"].includes(m.status) && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => deleteModel(m.id)}
                            className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 text-xs uppercase tracking-[0.2em]"
                            data-testid={`delete-model-btn-${m.id}`}
                          >
                            Delete
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </TabsContent>

          {/* SIMULATED TAB (existing) */}
          <TabsContent value="simulated" className="space-y-6">

        {/* Train panel */}
        <div className="panel p-6 mb-10">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// Configure run</div>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div>
              <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Dataset Version</Label>
              <Select value={selectedVersion} onValueChange={setSelectedVersion}>
                <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="train-version-select">
                  <SelectValue placeholder="Select version" />
                </SelectTrigger>
                <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                  {versions.map((v) => (
                    <SelectItem key={v.id} value={v.id}>{v.name} ({v.image_count} imgs)</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Architecture</Label>
              <Select value={modelArch} onValueChange={setModelArch}>
                <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid="train-arch-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                  <SelectItem value="yolov8n">YOLOv8 nano</SelectItem>
                  <SelectItem value="yolov8s">YOLOv8 small</SelectItem>
                  <SelectItem value="yolov8m">YOLOv8 medium</SelectItem>
                  <SelectItem value="yolov8l">YOLOv8 large</SelectItem>
                  <SelectItem value="rt-detr">RT-DETR</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Epochs</Label>
              <Input
                type="number"
                min="1"
                max="200"
                value={epochs}
                onChange={(e) => setEpochs(e.target.value)}
                className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
                data-testid="train-epochs-input"
              />
            </div>
            <div className="flex items-end">
              <Button
                onClick={train}
                disabled={training || !selectedVersion}
                className="w-full rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="start-training-btn"
              >
                <Play className="w-4 h-4 mr-2" /> {training ? "Training..." : "Start Training"}
              </Button>
            </div>
          </div>
          {versions.length === 0 && (
            <div className="text-xs text-muted-foreground mt-4">
              Create a dataset version first from the Versions tab.
            </div>
          )}
        </div>

        {/* Metrics */}
        {selectedModel && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-0 border-l border-t border-[#27272A] mb-6">
              <div className="border-r border-b border-[#27272A] p-5">
                <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">mAP@0.5</div>
                <div className="font-heading text-3xl font-bold text-primary" data-testid="metric-map">
                  {(selectedModel.final_mAP * 100).toFixed(1)}%
                </div>
              </div>
              <div className="border-r border-b border-[#27272A] p-5">
                <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Final Loss</div>
                <div className="font-heading text-3xl font-bold" data-testid="metric-loss">
                  {selectedModel.final_loss.toFixed(3)}
                </div>
              </div>
              <div className="border-r border-b border-[#27272A] p-5">
                <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Architecture</div>
                <div className="font-heading text-lg font-bold pt-1">{selectedModel.model_arch}</div>
              </div>
              <div className="border-r border-b border-[#27272A] p-5">
                <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Status</div>
                <div className="flex items-center gap-2 pt-1">
                  <div className="w-2 h-2 bg-primary rounded-full animate-pulse" />
                  <span className="text-sm font-bold uppercase">{selectedModel.status}</span>
                </div>
              </div>
            </div>

            {/* Chart */}
            <div className="panel p-6 mb-10">
              <div className="flex items-center justify-between mb-6">
                <div>
                  <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-1">// Training curves</div>
                  <h3 className="font-heading text-xl font-semibold">Metrics over epochs</h3>
                </div>
                <TrendingUp className="w-5 h-5 text-primary" />
              </div>
              <ResponsiveContainer width="100%" height={340}>
                <LineChart data={selectedModel.metrics}>
                  <CartesianGrid stroke="#27272A" strokeDasharray="3 3" />
                  <XAxis dataKey="epoch" stroke="#A1A1AA" style={{ fontFamily: "JetBrains Mono", fontSize: 10 }} />
                  <YAxis stroke="#A1A1AA" style={{ fontFamily: "JetBrains Mono", fontSize: 10 }} />
                  <Tooltip
                    contentStyle={{ background: "#121212", border: "1px solid #27272A", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 11 }}
                  />
                  <Legend wrapperStyle={{ fontFamily: "JetBrains Mono", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.1em" }} />
                  <Line type="monotone" dataKey="mAP" stroke="#06B6D4" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="loss" stroke="#EF4444" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="precision" stroke="#D946EF" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="recall" stroke="#EAB308" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>

            {/* Endpoint */}
            <div className="panel p-6 mb-10">
              <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Inference endpoint</div>
              <div className="flex items-center gap-3">
                <code className="flex-1 bg-[#050505] border border-[#27272A] px-4 py-3 text-xs overflow-x-auto" data-testid="model-endpoint">
                  POST {process.env.REACT_APP_BACKEND_URL}{selectedModel.endpoint}
                </code>
                <Button
                  variant="outline"
                  onClick={copyEndpoint}
                  className="rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em] h-11"
                  data-testid="copy-endpoint-btn"
                >
                  <Copy className="w-4 h-4 mr-2" /> Copy
                </Button>
              </div>
              <div className="mt-3 text-xs text-muted-foreground">
                Send POST with multipart image · Returns bounding boxes with labels & confidences.
              </div>
            </div>
          </>
        )}

        {/* All runs */}
        {models.filter((m) => !isTrainedModel(m)).length > 0 && (
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4">
              All runs ({models.filter((m) => !isTrainedModel(m)).length})
            </div>
            <div className="border-t border-[#27272A]">
              {models.filter((m) => !isTrainedModel(m)).map((m) => (
                <button
                  key={m.id}
                  onClick={() => setSelectedModel(m)}
                  className={`w-full text-left border-b border-[#27272A] p-5 flex items-center justify-between gap-4 transition-colors ${
                    selectedModel?.id === m.id ? "bg-[#121212] border-l-2 border-l-primary" : "hover:bg-[#1C1C1C]"
                  }`}
                  data-testid={`model-run-${m.id}`}
                >
                  <div className="flex items-center gap-4">
                    <Cpu className="w-4 h-4 text-primary" />
                    <div>
                      <div className="font-heading font-medium text-sm">{m.model_arch} · {m.epochs} epochs</div>
                      <div className="text-xs text-muted-foreground">{new Date(m.created_at).toLocaleString()}</div>
                    </div>
                  </div>
                  <div className="flex items-center gap-6 text-xs">
                    <span className="text-primary font-bold">mAP {(m.final_mAP * 100).toFixed(1)}%</span>
                    <span className="text-muted-foreground">loss {m.final_loss.toFixed(3)}</span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}

        {models.length === 0 && !selectedModel && (
          <div className="border border-dashed border-[#27272A] p-16 text-center">
            <Rocket className="w-10 h-10 text-muted-foreground mx-auto mb-6" strokeWidth={1.5} />
            <h3 className="font-heading text-xl mb-2">No models trained</h3>
            <p className="text-sm text-muted-foreground">Configure a run above to train your first model.</p>
          </div>
        )}
          </TabsContent>

          {/* DEPLOY PIPELINE TAB (M9) */}
          <TabsContent value="pipeline">
            <DeployPipeline pid={pid} project={project} />
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
