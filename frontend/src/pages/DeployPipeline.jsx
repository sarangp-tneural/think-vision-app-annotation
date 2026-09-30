import { useEffect, useRef, useState } from "react";
import { api, API, getToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import {
  Play, RotateCcw, Trash2, Server, CheckCircle2, XCircle, Loader2, AlertCircle, History, Lock,
} from "lucide-react";
import { DEPLOY_PIPELINE as T } from "@/constants/testIds";

// Defaults follow the team's manual fine-tuning recipe (train.py on the server).
const HP_DEFAULTS = {
  imgsz: 960, batch: 16, lr0: 0.001, lrf: 0.01, optimizer: "AdamW", weight_decay: 0.0005,
  mosaic: 0.5, close_mosaic: 10, mixup: 0.0, copy_paste: 0.0, degrees: 3.0, translate: 0.1,
  scale: 0.5, shear: 2.0, fliplr: 0.5, flipud: 0.0, device: "0", workers: 8, amp: true,
  cache: false, val: true, plots: true, save: true, save_period: 5, patience: 7, seed: 42,
};
const HP_GROUPS = [
  { title: "Training", keys: ["imgsz", "batch"] },
  { title: "Fine-tuning", keys: ["lr0", "lrf"] },
  { title: "Optimizer", keys: ["optimizer", "weight_decay"] },
  { title: "Augmentation", keys: ["mosaic", "close_mosaic", "mixup", "copy_paste", "degrees", "translate", "scale", "shear", "fliplr", "flipud"] },
  { title: "Performance", keys: ["device", "workers", "amp", "cache"] },
  { title: "Saving & stopping", keys: ["save_period", "patience", "seed", "val", "plots", "save"] },
];
const HP_OPTIMIZERS = ["SGD", "Adam", "Adamax", "AdamW", "NAdam", "RAdam", "RMSProp", "auto"];

// Non-secret fields persisted on the run (mirrors FORM_KEYS in
// backend/routers/deployment.py). password / pem_key never leave the browser
// except in stage-call bodies.
const FORM_KEYS = [
  "host", "port", "username", "remote_workdir", "remote_data_yaml_path",
  "remote_base_model_path", "remote_production_model_path",
  "train_pct", "valid_pct", "test_pct", "dataset_mode", "existing_data_yaml_path",
  "extra_yaml_path", "epochs", "env_mode", "venv_path", "model_choice",
  "yolo_model", "start_from_path", "hyperparams",
];

const TERMINAL_STATUSES = ["completed", "rejected"];
const BUSY_STAGE_LABEL = {
  uploading_data: "Uploading dataset to remote host...",
  class_check: "Checking remote classes...",
  training_remote: "Training on remote host...",
  downloading_model: "Downloading trained model...",
  testing: "Running video test...",
  deploying: "Rotating backups and deploying...",
};

// Ordered pipeline steps shown in the tracker. `stage` is the callable backend
// stage; "approve" is the human-review step (approve/reject API, not a stage).
const PIPELINE_STEPS = [
  { key: "uploading_data", label: "Upload" },
  { key: "class_check", label: "Class Check" },
  { key: "training_remote", label: "Train" },
  { key: "downloading_model", label: "Download" },
  { key: "testing", label: "Test" },
  { key: "approve", label: "Approve" },
  { key: "deploying", label: "Deploy" },
];

// A step is done only if its latest success is newer than every entry of the
// earlier steps - re-running step N therefore un-does steps after it.
function getStepStates(run) {
  const hist = run.stage_history || [];
  const lastIdx = (stage, onlyOk) => {
    for (let i = hist.length - 1; i >= 0; i--) {
      if (hist[i].stage === stage && (!onlyOk || hist[i].status === "succeeded")) return i;
    }
    return -1;
  };
  const bootstrap = run.run_type === "bootstrap";
  const rejected = run.approval?.decision === "rejected" || run.status === "rejected";
  const done = [];
  let floor = -1; // history index of the previous step's success
  PIPELINE_STEPS.forEach((step, i) => {
    let ok = false;
    if (step.key === "approve") {
      ok = done[i - 1] && run.approval?.decision === "approved";
    } else if (step.key === "deploying") {
      ok = !bootstrap && done[i - 1] && (lastIdx("deploying", true) > floor || run.status === "completed");
    } else {
      const at = lastIdx(step.key, true);
      ok = at > floor && (i === 0 || done[i - 1]);
      if (ok) floor = at;
    }
    done.push(!!ok);
  });
  const na = PIPELINE_STEPS.map((step) => step.key === "deploying" && bootstrap);
  const usable = PIPELINE_STEPS.map((_, i) => i).filter((i) => !na[i]);
  let currentIdx = usable.find((i) => !done[i]);
  if (currentIdx === undefined) currentIdx = usable[usable.length - 1];
  if (rejected) currentIdx = 5;
  const unlocked = PIPELINE_STEPS.map((_, i) => !na[i] && i <= currentIdx);
  return { done, na, unlocked, currentIdx, rejected };
}

function ProgressBar({ pct }) {
  return (
    <div className="mt-2 h-1 bg-[#27272A]" data-testid={T.progressBar}>
      <div
        className={`h-full bg-primary ${pct == null ? "animate-pulse w-full" : "transition-all"}`}
        style={pct != null ? { width: `${pct}%` } : undefined}
      />
    </div>
  );
}

function FormField({ label, children }) {
  return (
    <div>
      <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">{label}</Label>
      {children}
    </div>
  );
}

const inputClass = "rounded-sm bg-transparent border-[#27272A] h-11 focus-visible:ring-primary focus-visible:ring-2";
const pemClass = "w-full rounded-sm bg-transparent border border-[#27272A] p-3 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-primary focus:border-primary";

export default function DeployPipeline({ pid, project }) {
  const [pipelineData, setPipelineData] = useState({ runs: [], last_deployed_run_id: null, pipeline_id: null });
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // Step being viewed in the tracker; null = follow the run's current step.
  const [viewStep, setViewStep] = useState(null);
  const [models, setModels] = useState(null);
  const [hydratedFor, setHydratedFor] = useState(null);
  const savedFormRef = useRef({ runId: null, json: "" });
  const [testFile, setTestFile] = useState(null);
  const [testConf, setTestConf] = useState(0.25);
  // Fresh SSH creds + every stage-specific field live in one object, kept in
  // local component state only - never persisted to localStorage or sent
  // anywhere but the immediate stage-call body, matching the backend's own
  // "never persisted" design. Pydantic ignores unknown keys, so it's safe to
  // send this whole object as the body for every stage/approve/reject/
  // rollback call rather than hand-picking a subset per stage.
  const [fields, setFields] = useState({
    host: "", port: 22, username: "", password: "", pem_key: "",
    remote_workdir: "", remote_data_yaml_path: "",
    remote_base_model_path: "", remote_production_model_path: "",
    train_pct: 0.7, valid_pct: 0.2, test_pct: 0.1, epochs: 30,
    env_mode: "system", venv_path: "",
    model_choice: "yolo", yolo_model: "yolov8n.pt",
    dataset_mode: "new", existing_data_yaml_path: "", extra_yaml_path: "",
  });
  const [remoteInfo, setRemoteInfo] = useState(null);
  const [hp, setHp] = useState(HP_DEFAULTS);
  const setHpField = (key) => (e) => setHp((h) => ({ ...h, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const setField = (key) => (e) => setFields((f) => ({ ...f, [key]: e.target.value }));
  // Numeric fields come off <input> as strings; coerce before every request
  // body so the backend's Pydantic int/float fields don't have to guess.
  const NUMERIC_FIELDS = ["port", "epochs", "train_pct", "valid_pct", "test_pct"];
  const buildBody = () => {
    const body = { ...fields };
    for (const key of NUMERIC_FIELDS) if (body[key] !== "") body[key] = Number(body[key]);
    // Only the project directory is asked for: the production model lives in
    // {workdir}/models by default, and the starting model is either an
    // existing remote checkpoint or a fresh YOLO model chosen in the UI.
    const wd = (body.remote_workdir || "").replace(/\/+$/, "");
    if (!body.remote_production_model_path && wd) body.remote_production_model_path = `${wd}/models/production.pt`;
    const hyper = {};
    for (const [k, v] of Object.entries(hp)) {
      if (v === "" || v === null || v === undefined) continue;
      hyper[k] = typeof v === "boolean" || k === "optimizer" || k === "device" ? v : Number(v);
    }
    body.hyperparams = hyper;
    body.start_from_path = body.model_choice !== "yolo" ? body.model_choice : "";
    body.yolo_model = body.model_choice === "yolo" ? body.yolo_model : "";
    return body;
  };

  const loadPipeline = async () => {
    try {
      const { data } = await api.get(`/projects/${pid}/pipeline/runs`);
      setPipelineData(data);
      api.get(`/projects/${pid}/models`)
        .then((r) => setModels(r.data))
        .catch(() => {});
      // The project directory is remembered server-side; prefill it once.
      if (data.remote_workdir) {
        setFields((f) => (f.remote_workdir ? f : { ...f, remote_workdir: data.remote_workdir }));
      }
      setSelectedRunId((prev) => {
        if (prev && data.runs.some((r) => r.id === prev)) return prev;
        return data.runs[0]?.id || null;
      });
    } catch (e) {
      console.debug("pipeline load failed", e);
    }
  };

  useEffect(() => { loadPipeline(); /* eslint-disable-next-line */ }, [pid]);

  useEffect(() => {
    const iv = setInterval(loadPipeline, 5000);
    return () => clearInterval(iv);
    // eslint-disable-next-line
  }, [pid]);

  const selectedRun = pipelineData.runs.find((r) => r.id === selectedRunId) || null;
  const runModel = selectedRun?.candidate_model
    ? (models || []).find((m) => m.id === selectedRun.candidate_model.local_model_id) || null
    : null;

  // Fill the form from what this run saved in the database (once per run
  // selection - not on every poll, which would clobber what's being typed).
  // Secrets stay whatever is in memory.
  useEffect(() => {
    if (!selectedRun || hydratedFor === selectedRun.id) return;
    const { hyperparams, ...saved } = selectedRun.form || {};
    setFields((f) => ({ ...f, ...saved }));
    setHp({ ...HP_DEFAULTS, ...(hyperparams || {}) });
    setHydratedFor(selectedRun.id);
    // eslint-disable-next-line
  }, [selectedRun?.id]);

  // Debounced auto-save of the (non-secret) form to the run.
  useEffect(() => {
    if (!selectedRunId || hydratedFor !== selectedRunId) return undefined;
    const full = buildBody();
    const payload = {};
    for (const k of FORM_KEYS) if (full[k] !== undefined && full[k] !== "") payload[k] = full[k];
    const json = JSON.stringify(payload);
    const saved = savedFormRef.current;
    if (saved.runId !== selectedRunId) {
      savedFormRef.current = { runId: selectedRunId, json };
      return undefined;
    }
    if (saved.json === json) return undefined;
    const t = setTimeout(() => {
      api.put(`/pipeline/runs/${selectedRunId}/form`, payload)
        .then(() => { savedFormRef.current = { runId: selectedRunId, json }; })
        .catch(() => {});
    }, 800);
    return () => clearTimeout(t);
    // eslint-disable-next-line
  }, [fields, hp, hydratedFor, selectedRunId]);

  // Snap back to the run's current step when switching runs or when a stage
  // starts/finishes, so the view auto-advances after each step completes.
  const historyLen = selectedRun?.stage_history?.length;
  useEffect(() => { setViewStep(null); }, [selectedRunId, selectedRun?.status, selectedRun?.busy, historyLen]);

  const startNewRun = async () => {
    setSubmitting(true);
    try {
      const { data } = await api.post(`/projects/${pid}/pipeline/runs`);
      toast.success(`New ${data.run_type} run created`);
      await loadPipeline();
      setSelectedRunId(data.id);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not start a new run");
    } finally {
      setSubmitting(false);
    }
  };

  const callStage = async (rid, stage) => {
    setSubmitting(true);
    try {
      await api.post(`/pipeline/runs/${rid}/stage/${stage}`, buildBody());
      toast.success(`${stage.replace("_", " ")} started`);
      await loadPipeline();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Something went wrong");
    } finally {
      setSubmitting(false);
    }
  };

  const runVideoTest = async (rid) => {
    if (!testFile) { toast.error("Choose a video first"); return; }
    setSubmitting(true);
    try {
      const form = new FormData();
      form.append("file", testFile);
      form.append("conf", String(Number(testConf) || 0.25));
      await api.post(`/pipeline/runs/${rid}/test-video`, form);
      toast.success("Video test started");
      setTestFile(null);
      await loadPipeline();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Video test failed to start");
    } finally {
      setSubmitting(false);
    }
  };

  const testVideoUrl = (rid, testId) =>
    `${API}/pipeline/runs/${rid}/test-video/${testId}?auth=${encodeURIComponent(getToken() || "")}`;

  const renderTestVideos = (run) => (run.video_tests || []).length > 0 && (
    <div className="space-y-4">
      {run.video_tests.map((t) => (
        <div key={t.id} className="space-y-2" data-testid={`${T.testVideo}-${t.id}`}>
          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
            {t.input_name} · conf {t.conf}{t.frames ? ` · ${t.frames} frames` : ""} · {t.status}
          </div>
          {t.status === "succeeded" && (
            <video src={testVideoUrl(run.id, t.id)} controls preload="metadata" className="w-full max-h-[420px] bg-black border border-[#27272A]" />
          )}
          {t.status === "failed" && <div className="text-xs text-destructive break-words">{t.error}</div>}
        </div>
      ))}
    </div>
  );

  const inspectRemote = async (rid) => {
    setSubmitting(true);
    try {
      const { data } = await api.post(`/pipeline/runs/${rid}/inspect_remote`, buildBody());
      setRemoteInfo(data);
      setFields((f) => ({
        ...f,
        remote_workdir: data.workdir || f.remote_workdir,
        existing_data_yaml_path: data.found
          ? (data.datasets.some((d) => d.data_yaml_path === f.existing_data_yaml_path)
            ? f.existing_data_yaml_path : data.datasets[0].data_yaml_path)
          : "",
        dataset_mode: data.found ? (f.dataset_mode === "new" ? "merge" : f.dataset_mode) : "new",
      }));
      toast.success(data.found ? `${data.datasets.length} dataset(s) found` : "No existing dataset found");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not inspect remote host");
    } finally {
      setSubmitting(false);
    }
  };

  // Lists models already on the server without touching dataset choices.
  const scanModels = async (rid) => {
    setSubmitting(true);
    try {
      const { data } = await api.post(`/pipeline/runs/${rid}/inspect_remote`, buildBody());
      setRemoteInfo(data);
      toast.success(`${data.models?.length || 0} model(s) found on the server`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not scan remote host");
    } finally {
      setSubmitting(false);
    }
  };

  // Uploading straight away would copy the whole dataset into a fresh run
  // folder even when it's already on the server, so look first.
  const uploadDataset = async (rid) => {
    if (!remoteInfo) {
      setSubmitting(true);
      try {
        const { data } = await api.post(`/pipeline/runs/${rid}/inspect_remote`, buildBody());
        setRemoteInfo(data);
        if (data.found) {
          setFields((f) => ({ ...f, existing_data_yaml_path: data.datasets[0].data_yaml_path, dataset_mode: "merge" }));
          toast.info("Existing dataset found on the server — choose how to use it, then press Upload again");
          return;
        }
      } catch (e) {
        toast.error(e.response?.data?.detail || "Could not inspect remote host");
        return;
      } finally {
        setSubmitting(false);
      }
    } else if (remoteInfo.found && fields.dataset_mode === "new"
      && !window.confirm("A dataset already exists on the server. Upload a fresh full copy anyway?")) {
      return;
    }
    await callStage(rid, "uploading_data");
  };

  const deleteRun = async (rid) => {
    if (!window.confirm("Delete this run from history? This does not touch the remote server.")) return;
    setSubmitting(true);
    try {
      await api.delete(`/pipeline/runs/${rid}`);
      toast.success("Run deleted");
      if (selectedRunId === rid) setSelectedRunId(null);
      await loadPipeline();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Delete failed");
    } finally {
      setSubmitting(false);
    }
  };

  const approve = async (rid) => {
    setSubmitting(true);
    try {
      const { data } = await api.post(`/pipeline/runs/${rid}/approve`);
      toast.success(data.status === "completed" ? "Approved" : "Approved — ready to deploy");
      await loadPipeline();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Approve failed");
    } finally {
      setSubmitting(false);
    }
  };

  const reject = async (rid) => {
    const note = window.prompt("Reason for rejection?") || "";
    setSubmitting(true);
    try {
      await api.post(`/pipeline/runs/${rid}/reject`, { note });
      toast.success("Run rejected");
      await loadPipeline();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Reject failed");
    } finally {
      setSubmitting(false);
    }
  };

  const submitRollback = async () => {
    setSubmitting(true);
    try {
      const { data } = await api.post(`/projects/${pid}/pipeline/rollback`, buildBody());
      toast.success("Rollback started");
      setRollbackOpen(false);
      await loadPipeline();
      setSelectedRunId(data.id);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Rollback failed");
    } finally {
      setSubmitting(false);
    }
  };

  const SSHFields = ({ withPem = true } = {}) => (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
      <FormField label="Host">
        <Input value={fields.host} onChange={setField("host")} className={inputClass} data-testid={T.hostInput} />
      </FormField>
      <FormField label="Port">
        <Input type="number" value={fields.port} onChange={setField("port")} className={inputClass} data-testid={T.portInput} />
      </FormField>
      <FormField label="Username">
        <Input value={fields.username} onChange={setField("username")} className={inputClass} data-testid={T.usernameInput} />
      </FormField>
      <FormField label="Password / passphrase">
        <Input type="password" value={fields.password} onChange={setField("password")} className={inputClass} data-testid={T.passwordInput} />
      </FormField>
      {withPem && (
        <div className="md:col-span-4">
          <FormField label="PEM private key (optional — leave blank to use password auth)">
            <textarea
              value={fields.pem_key}
              onChange={setField("pem_key")}
              placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"}
              rows={6}
              className={pemClass}
              data-testid={T.pemInput}
            />
          </FormField>
        </div>
      )}
    </div>
  );

  const RadioGroup = ({ name, value, onChange, options, testId }) => (
    <div className="flex flex-wrap gap-x-6 gap-y-2" data-testid={testId}>
      {options.map((o) => (
        <label key={o.value} className="flex items-center gap-2 text-xs cursor-pointer">
          <input
            type="radio" name={name} value={o.value} checked={value === o.value}
            onChange={() => onChange(o.value)} className="accent-primary"
          />
          {o.label}
        </label>
      ))}
    </div>
  );

  const StageButton = ({ label, onClick, disabled }) => (
    <Button
      onClick={onClick}
      disabled={submitting || disabled}
      className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 text-xs uppercase tracking-[0.2em] font-bold"
      data-testid={T.stageSubmitButton}
    >
      <Play className="w-4 h-4 mr-2" /> {label}
    </Button>
  );

  // One source of truth per stage: the same inputs are shown when a stage is
  // about to run and again (unchanged) when it failed and is being retried.
  const uploadFields = (run) => (
    <>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <FormField label="Remote workdir">
                <Input value={fields.remote_workdir} onChange={setField("remote_workdir")} className={inputClass} placeholder="/srv/pipeline" data-testid={T.remoteWorkdirInput} />
              </FormField>
              <FormField label="Train %">
                <Input type="number" step="0.05" value={fields.train_pct} onChange={setField("train_pct")} className={inputClass} data-testid={T.trainPctInput} />
              </FormField>
              <FormField label="Valid %">
                <Input type="number" step="0.05" value={fields.valid_pct} onChange={setField("valid_pct")} className={inputClass} data-testid={T.validPctInput} />
              </FormField>
              <FormField label="Test %">
                <Input type="number" step="0.05" value={fields.test_pct} onChange={setField("test_pct")} className={inputClass} data-testid={T.testPctInput} />
              </FormField>
            </div>
            <div className="space-y-3">
              <Button
                variant="outline" size="sm" disabled={submitting || !fields.remote_workdir}
                onClick={() => inspectRemote(run.id)}
                className="rounded-sm h-8 text-xs uppercase tracking-[0.2em]"
                data-testid={T.inspectRemoteButton}
              >
                Check remote for existing dataset
              </Button>
              <div className="flex gap-2 items-end">
                <div className="flex-1">
                  <FormField label="Or a specific data.yaml path (any name, anywhere on the server)">
                    <Input value={fields.extra_yaml_path} onChange={setField("extra_yaml_path")} className={inputClass} placeholder="/srv/datasets/my_set/roboflow.yaml" data-testid={T.extraYamlInput} />
                  </FormField>
                </div>
              </div>
              {remoteInfo?.found && (
                <div className="p-3 border border-[#27272A] text-xs space-y-3">
                  <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Datasets found — pick one</div>
                  {remoteInfo.datasets.map((d) => (
                    <label key={d.data_yaml_path} className="flex items-start gap-2 cursor-pointer" data-testid={T.datasetOption}>
                      <input
                        type="radio" name="existing_dataset" className="accent-primary mt-0.5"
                        checked={fields.existing_data_yaml_path === d.data_yaml_path}
                        onChange={() => setFields((f) => ({ ...f, existing_data_yaml_path: d.data_yaml_path }))}
                      />
                      <span>
                        <code className="text-primary break-all">{d.data_yaml_path}</code>
                        <span className="block text-muted-foreground">
                          classes: {d.names.join(", ") || "—"} · {Object.entries(d.splits).map(([k, v]) => `${k}: ${v.image_count}`).join(" · ")} images
                        </span>
                      </span>
                    </label>
                  ))}
                  <RadioGroup
                    name="dataset_mode" value={fields.dataset_mode} testId={T.datasetModeRadio}
                    onChange={(v) => setFields((f) => ({ ...f, dataset_mode: v }))}
                    options={[
                      { value: "merge", label: "Add my images into it (per its yaml)" },
                      { value: "reuse", label: "Use it as-is (no upload)" },
                      { value: "new", label: "Ignore, upload a fresh dataset" },
                    ]}
                  />
                </div>
              )}
              {remoteInfo && !remoteInfo.found && (
                <div className="text-xs text-muted-foreground">No dataset yaml found under the workdir — a fresh dataset will be uploaded.</div>
              )}
            </div>
    </>
  );

  const classCheckFields = (run) => {
    const de = run.dataset_export;
    return (
      <>
            <FormField label="Remote data.yaml path (optional — defaults to the uploaded dataset's yaml)">
              <Input value={fields.remote_data_yaml_path} onChange={setField("remote_data_yaml_path")} className={inputClass} placeholder={de?.data_yaml_path || de?.remote_upload_path ? (de.data_yaml_path || `${de.remote_upload_path}/data.yaml`) : "/srv/data/data.yaml"} data-testid={T.remoteDataYamlInput} />
            </FormField>
      </>
    );
  };

  const trainingFields = (run) => (
    <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <FormField label="Remote project directory">
                <Input value={fields.remote_workdir} onChange={setField("remote_workdir")} className={inputClass} placeholder="/root/testing_pipeline" data-testid={T.remoteWorkdirInput} />
              </FormField>
              <FormField label="Epochs">
                <Input type="number" value={fields.epochs} onChange={setField("epochs")} className={inputClass} data-testid={T.epochsInput} />
              </FormField>
            </div>
            <div className="space-y-3 border border-[#27272A] p-4" data-testid={T.trainingSettings}>
              <div className="flex items-center justify-between">
                <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Training settings</div>
                <Button
                  variant="outline" size="sm" onClick={() => setHp(HP_DEFAULTS)}
                  className="rounded-sm h-7 text-[10px] uppercase tracking-[0.2em]"
                >
                  Reset to defaults
                </Button>
              </div>
              {HP_GROUPS.map((g) => (
                <div key={g.title} className="space-y-2">
                  <div className="text-[10px] text-primary">{g.title}</div>
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                    {g.keys.map((k) => (
                      typeof HP_DEFAULTS[k] === "boolean" ? (
                        <label key={k} className="flex items-center gap-2 text-xs cursor-pointer pt-6">
                          <input type="checkbox" checked={!!hp[k]} onChange={setHpField(k)} className="accent-primary" />
                          {k}
                        </label>
                      ) : k === "optimizer" ? (
                        <FormField key={k} label={k}>
                          <select value={hp[k]} onChange={setHpField(k)} className={`${inputClass} px-3 w-full`}>
                            {HP_OPTIMIZERS.map((o) => <option key={o} value={o} className="bg-[#0a0a0a]">{o}</option>)}
                          </select>
                        </FormField>
                      ) : (
                        <FormField key={k} label={k}>
                          <Input
                            type={k === "device" ? "text" : "number"} step="any" value={hp[k]}
                            onChange={setHpField(k)} className={inputClass}
                            placeholder={k === "device" ? "0 / cpu / blank=auto" : undefined}
                          />
                        </FormField>
                      )
                    ))}
                  </div>
                </div>
              ))}
              <div className="text-[10px] text-muted-foreground">
                device: 0 = first GPU, 0,1 = several, cpu, or blank for auto (use cpu/blank on a server without a GPU).
              </div>
            </div>
            <FormField label="Starting model">
              <div className="space-y-3">
                <Button
                  variant="outline" size="sm" disabled={submitting || !fields.remote_workdir}
                  onClick={() => scanModels(run.id)}
                  className="rounded-sm h-8 text-xs uppercase tracking-[0.2em]"
                  data-testid={T.scanModelsButton}
                >
                  Scan server for existing models
                </Button>
                {remoteInfo?.models?.length > 0 && (
                  <div className="space-y-2" data-testid={T.modelChoiceRadio}>
                    <div className="text-[10px] text-muted-foreground">Continue training from a model already on the server:</div>
                    {remoteInfo.models.map((m) => (
                      <label key={m.path} className="flex items-start gap-2 text-xs cursor-pointer">
                        <input
                          type="radio" name="model_choice" className="accent-primary mt-0.5"
                          checked={fields.model_choice === m.path}
                          onChange={() => setFields((f) => ({ ...f, model_choice: m.path }))}
                        />
                        <span>
                          <code className="text-primary break-all">{m.path}</code>
                          <span className="block text-muted-foreground">
                            {(m.size / 1048576).toFixed(1)} MB · {new Date(m.mtime * 1000).toLocaleString()}
                          </span>
                        </span>
                      </label>
                    ))}
                  </div>
                )}
                {remoteInfo && !remoteInfo.models?.length && (
                  <div className="text-xs text-muted-foreground">No trained models found under the project directory.</div>
                )}
                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input
                    type="radio" name="model_choice" className="accent-primary"
                    checked={fields.model_choice === "yolo"}
                    onChange={() => setFields((f) => ({ ...f, model_choice: "yolo" }))}
                  />
                  Start fresh from a YOLO model (downloaded on the server)
                </label>
                {fields.model_choice === "yolo" && (
                  <select
                    value={fields.yolo_model} onChange={setField("yolo_model")}
                    className={`${inputClass} px-3 w-full md:w-64`} data-testid={T.yoloModelSelect}
                  >
                    {["yolov8n.pt", "yolov8s.pt", "yolov8m.pt", "yolov8l.pt", "yolov8x.pt",
                      "yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolo11l.pt", "yolo11x.pt"].map((n) => (
                      <option key={n} value={n} className="bg-[#0a0a0a]">{n}</option>
                    ))}
                  </select>
                )}
              </div>
            </FormField>
            <FormField label="Python environment on remote host">
              <RadioGroup
                name="env_mode" value={fields.env_mode} testId={T.envModeRadio}
                onChange={(v) => setFields((f) => ({ ...f, env_mode: v }))}
                options={[
                  { value: "system", label: "System python3" },
                  { value: "existing", label: "Existing venv (source its bin/activate)" },
                  { value: "create", label: "Create venv in workdir & install dependencies" },
                ]}
              />
            </FormField>
            {fields.env_mode === "existing" && (
              <FormField label="Venv folder or activate script (e.g. /venvs/training or /venvs/training/bin/activate)">
                <Input value={fields.venv_path} onChange={setField("venv_path")} className={inputClass} placeholder="/venvs/training" data-testid={T.venvPathInput} />
              </FormField>
            )}
            {fields.env_mode === "create" && (
              <div className="text-[10px] text-muted-foreground">
                Creates <code>{(fields.remote_workdir || "<workdir>").replace(/\/$/, "")}/venv</code> if missing and pip-installs ultralytics (reused on later runs).
              </div>
            )}
    </>
  );

  const deployFields = () => (
    <>
              <FormField label="Remote production model path">
                <Input value={fields.remote_production_model_path} onChange={setField("remote_production_model_path")} className={inputClass} placeholder={`${(fields.remote_workdir || "<workdir>").replace(/\/+$/, "")}/models/production.pt (default)`} data-testid={T.remoteProductionModelInput} />
              </FormField>
    </>
  );

  const testFields = (run) => (
    <div className="space-y-4">
      <div className="text-xs text-muted-foreground max-w-xl">
        Upload a video to run the downloaded model over every frame. The annotated video plays below so you can judge it.
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="sm:col-span-2">
          <FormField label="Test video (mp4, mov, webm, avi, mkv · max 200MB)">
            <input
              type="file" accept="video/*"
              onChange={(e) => setTestFile(e.target.files?.[0] || null)}
              className="block w-full text-xs file:mr-3 file:rounded-sm file:border file:border-[#27272A] file:bg-transparent file:px-3 file:py-2 file:text-xs file:text-foreground"
              data-testid={T.testVideoInput}
            />
          </FormField>
        </div>
        <FormField label="Confidence">
          <Input type="number" min="0.01" max="1" step="0.05" value={testConf} onChange={(e) => setTestConf(e.target.value)} className={inputClass} />
        </FormField>
      </div>
      <Button
        onClick={() => runVideoTest(run.id)}
        disabled={submitting || !testFile}
        variant="outline"
        className="rounded-sm border-primary text-primary bg-transparent hover:bg-primary/10 h-10 text-xs uppercase tracking-[0.2em] font-bold"
        data-testid={T.runVideoTestButton}
      >
        <Play className="w-4 h-4 mr-2" /> Run video test
      </Button>
      {renderTestVideos(run)}
    </div>
  );

  const STAGE_FORMS = {
    uploading_data: { label: "Upload Dataset", ssh: true, fields: uploadFields, action: (rid) => uploadDataset(rid) },
    class_check: { label: "Check Classes", ssh: true, fields: classCheckFields },
    training_remote: { label: "Start Training", ssh: true, fields: trainingFields },
    downloading_model: { label: "Download Model", ssh: true },
    testing: {
      label: "Finish testing → Approve", ssh: false, fields: testFields,
      disabled: (run) => !(run.video_tests || []).some((t) => t.status === "succeeded"),
    },
    deploying: { label: "Deploy", ssh: true, fields: deployFields },
  };

  const renderStageForm = (stage, run, labelOverride) => {
    const st = STAGE_FORMS[stage];
    if (!st) return null;
    return (
      <>
        {st.ssh && SSHFields()}
        {st.fields && st.fields(run)}
        <StageButton
          label={labelOverride || st.label}
          disabled={st.disabled ? st.disabled(run) : false}
          onClick={() => (st.action ? st.action(run.id) : callStage(run.id, stage))}
        />
      </>
    );
  };

  const renderStepSummary = (key, run) => {
    switch (key) {
      case "uploading_data": {
        const de = run.dataset_export;
        return de && (
          <div className="text-xs text-muted-foreground">
            Uploaded {de.train_count} train · {de.valid_count} valid · {de.test_count} test images to{" "}
            <code className="text-primary">{de.remote_upload_path}</code>
          </div>
        );
      }
      case "class_check": {
        const cc = run.class_check;
        return cc && (
          <div className={`text-xs p-3 border ${cc.match ? "border-[#22C55E] text-[#22C55E]" : "border-destructive text-destructive"}`}>
            {cc.match ? "Classes match." : `Class mismatch: ${JSON.stringify(cc.diff)}`}
          </div>
        );
      }
      case "training_remote": {
        const t = run.training;
        return t && (
          <div className="text-xs text-muted-foreground">
            Trained from <code className="text-primary">{t.base_checkpoint_ref}</code> in{" "}
            <code className="text-primary">{t.remote_run_dir}</code>
          </div>
        );
      }
      case "downloading_model": {
        const cm = run.candidate_model;
        return cm && (
          <div className="text-xs text-primary font-bold">
            Candidate mAP@50 {((cm.metrics?.mAP50 || 0) * 100).toFixed(1)}%
          </div>
        );
      }
      case "testing": {
        const n = (run.video_tests || []).filter((t) => t.status === "succeeded").length;
        return n > 0 && <div className="text-xs text-muted-foreground">{n} test video{n > 1 ? "s" : ""} reviewed.</div>;
      }
      case "approve":
        return run.approval?.decision && (
          <div className="text-xs p-3 border border-[#22C55E] text-[#22C55E]">
            {run.approval.decision === "approved" ? "Approved." : `Rejected: ${run.approval.note || "no reason given"}`}
          </div>
        );
      default:
        return null;
    }
  };

  const renderStepBody = (run) => {
    const stage = run.status;
    const busy = run.busy;
    const st = getStepStates(run);
    const videoRunning = (run.video_tests || []).some((t) => t.status === "running");

    // Viewing an earlier (or otherwise non-current) unlocked step from the tracker.
    if (!busy && viewStep !== null && viewStep !== st.currentIdx && st.unlocked[viewStep]) {
      const step = PIPELINE_STEPS[viewStep];
      return (
        <div className="space-y-4">
          {renderStepSummary(step.key, run)}
          {STAGE_FORMS[step.key] && (
            <>
              {viewStep < st.currentIdx && (
                <div className="text-[10px] text-muted-foreground">
                  Re-running {step.label} will invalidate the steps after it.
                </div>
              )}
              {renderStageForm(step.key, run, `Re-run ${step.label}`)}
            </>
          )}
        </div>
      );
    }

    if (stage === "failed") {
      const lastStage = run.stage_history?.[run.stage_history.length - 1]?.stage;
      return (
        <div className="space-y-4">
          <div className="p-3 bg-[#050505] border border-destructive/50 flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-destructive shrink-0 mt-0.5" />
            <div className="text-xs text-destructive whitespace-pre-wrap break-words max-h-64 overflow-y-auto">{run.error || "This run failed."}</div>
          </div>
          {lastStage && renderStageForm(lastStage, run, `Retry ${lastStage.replace("_", " ")}`)}
        </div>
      );
    }

    if (busy) {
      return (
        <div className="p-3 bg-[#050505] border border-[#27272A] flex items-center gap-3">
          <Loader2 className="w-5 h-5 text-primary animate-spin shrink-0" />
          <div className="flex-1">
            <div className="text-xs uppercase tracking-[0.2em] text-primary">{videoRunning ? "video test" : stage.replace("_", " ")}</div>
            <div className="text-[10px] text-muted-foreground">{videoRunning ? BUSY_STAGE_LABEL.testing : (BUSY_STAGE_LABEL[stage] || "Working...")}</div>
            {stage === "training_remote" && run.training && (
              <>
                <ProgressBar pct={run.training.progress_pct} />
                {run.training.log_tail && (
                  <pre className="mt-2 max-h-32 overflow-y-auto text-[10px] font-mono text-muted-foreground whitespace-pre-wrap">
                    {run.training.log_tail}
                  </pre>
                )}
              </>
            )}
            {stage === "uploading_data" && run.upload_progress && (
              <>
                <div className="mt-2 text-[10px] text-muted-foreground" data-testid={T.uploadProgressText}>
                  {run.upload_progress.phase === "preparing" ? "Preparing files" : "Uploaded"}{" "}
                  {run.upload_progress.done} / {run.upload_progress.total}
                  {run.upload_progress.total > 0 && ` (${Math.round(100 * run.upload_progress.done / run.upload_progress.total)}%)`}
                </div>
                <ProgressBar pct={run.upload_progress.total ? Math.round(100 * run.upload_progress.done / run.upload_progress.total) : null} />
              </>
            )}
            {stage === "testing" || (run.video_tests || []).some((t) => t.status === "running") ? (
              run.test_progress?.total ? (
                <>
                  <div className="mt-2 text-[10px] text-muted-foreground">
                    Frame {run.test_progress.done} / {run.test_progress.total}
                  </div>
                  <ProgressBar pct={Math.round(100 * run.test_progress.done / run.test_progress.total)} />
                </>
              ) : <ProgressBar pct={null} />
            ) : null}
            {stage !== "training_remote" && stage !== "testing" && !(run.video_tests || []).some((t) => t.status === "running") && !(stage === "uploading_data" && run.upload_progress) && <ProgressBar pct={null} />}
          </div>
        </div>
      );
    }

    switch (stage) {
      case "draft":
        return <div className="space-y-4">{renderStageForm("uploading_data", run)}</div>;

      case "uploading_data": {
        return (
          <div className="space-y-4">
            {renderStepSummary("uploading_data", run)}
            {renderStageForm("class_check", run)}
          </div>
        );
      }

      case "class_check": {
        return (
          <div className="space-y-4">
            {renderStepSummary("class_check", run)}
            {renderStageForm("training_remote", run)}
          </div>
        );
      }

      case "training_remote": {
        return (
          <div className="space-y-4">
            {renderStepSummary("training_remote", run)}
            {renderStageForm("downloading_model", run)}
          </div>
        );
      }

      case "downloading_model": {
        return (
          <div className="space-y-4">
            {renderStepSummary("downloading_model", run)}
            {renderStageForm("testing", run)}
          </div>
        );
      }

      case "awaiting_approval": {
        if (run.approval?.decision === "approved") {
          return (
            <div className="space-y-4">
              <div className="text-xs p-3 border border-[#22C55E] text-[#22C55E]">Approved — ready to deploy.</div>
              {renderStageForm("deploying", run)}
            </div>
          );
        }
        return (
          <div className="space-y-6">
            {renderTestVideos(run)}
            <div className="grid grid-cols-2 gap-2 max-w-md">
              <Button
                size="sm"
                disabled={submitting}
                onClick={() => approve(run.id)}
                className="rounded-sm bg-[#22C55E] text-black hover:bg-[#22C55E]/90 h-8 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid={T.approveButton}
              >
                <CheckCircle2 className="w-3 h-3 mr-1" /> Approve
              </Button>
              <Button
                size="sm"
                disabled={submitting}
                variant="outline"
                onClick={() => reject(run.id)}
                className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 h-8 text-xs uppercase tracking-[0.2em]"
                data-testid={T.rejectButton}
              >
                <XCircle className="w-3 h-3 mr-1" /> Reject
              </Button>
            </div>
          </div>
        );
      }

      case "completed": {
        const deploy = run.deploy;
        return (
          <div className="text-xs p-3 border border-[#22C55E] text-[#22C55E]">
            {run.run_type === "bootstrap"
              ? "Approved. Activate the model from Model Training when you want to use it for auto labelling."
              : deploy?.rolled_back
                ? "Rolled back — previous backup restored."
                : "Deployed to the remote host and activated locally."}
          </div>
        );
      }

      case "rejected":
        return (
          <div className="text-xs p-3 border border-[#27272A] bg-[#121212] text-muted-foreground">
            <div className="text-[9px] uppercase tracking-[0.2em] text-primary mb-1">Rejected</div>
            {run.approval?.note || "No reason given."}
          </div>
        );

      default:
        return null;
    }
  };

  const renderStepTracker = (run) => {
    const st = getStepStates(run);
    const active = viewStep ?? st.currentIdx;
    return (
      <div className="flex items-center overflow-x-auto pb-2 -mx-1" data-testid={T.stepTracker}>
        {PIPELINE_STEPS.map((step, i) => {
          const locked = !st.unlocked[i];
          const isDone = st.done[i];
          const rejectedHere = st.rejected && i === 5;
          const color = rejectedHere ? "text-destructive" : isDone ? "text-[#22C55E]" : locked ? "text-muted-foreground opacity-40" : "text-primary";
          return (
            <div key={step.key} className="flex items-center shrink-0">
              {i > 0 && <div className={`w-6 h-px mx-1 ${st.done[i - 1] ? "bg-[#22C55E]" : "bg-[#27272A]"}`} />}
              <button
                type="button"
                disabled={locked || run.busy}
                title={st.na[i] ? "Not applicable to bootstrap runs" : locked ? "Complete the previous step first" : step.label}
                onClick={() => setViewStep(i === st.currentIdx ? null : i)}
                className={`flex items-center gap-2 px-3 py-2 text-[10px] uppercase tracking-[0.2em] border-b-2 transition-colors ${color} ${
                  active === i && !locked ? "border-current bg-[#121212]" : "border-transparent"
                } ${locked ? "cursor-not-allowed" : "hover:bg-[#1C1C1C]"}`}
                data-testid={`${T.stepTab}-${step.key}`}
              >
                <span className="w-5 h-5 rounded-full border border-current flex items-center justify-center text-[9px] font-bold">
                  {isDone ? <CheckCircle2 className="w-3 h-3" /> : rejectedHere ? <XCircle className="w-3 h-3" /> : locked ? <Lock className="w-2.5 h-2.5" /> : i + 1}
                </span>
                <span>{step.label}</span>
              </button>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="space-y-8">
      <div className="panel p-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-2">// Remote deploy pipeline</div>
          <p className="text-xs text-muted-foreground max-w-xl">
            Upload → train → test → approve → deploy against a remote host over SSH, with one-click rollback.
            Credentials are supplied fresh at each step and never stored.
          </p>
          <div className="mt-3 text-xs" data-testid={T.activeModel}>
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mr-2">Model for this run</span>
            {runModel ? (
              <>
                <span className="text-primary font-bold">{runModel.name || runModel.model_arch || "server model"}</span>
                <span className="text-muted-foreground">
                  {" · trained from server"}
                  {runModel.final_mAP != null && ` · mAP@50 ${(runModel.final_mAP * 100).toFixed(1)}%`}
                  {runModel.is_active && " · active for auto-labeling"}
                </span>
              </>
            ) : selectedRun?.candidate_model && models !== null ? (
              <span className="font-bold text-destructive">Deleted <span className="font-normal text-muted-foreground">(run Download again)</span></span>
            ) : (
              <span className="text-muted-foreground">No model downloaded for this run yet</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            disabled={!pipelineData.last_deployed_run_id || submitting}
            onClick={() => setRollbackOpen((v) => !v)}
            className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 h-10 text-xs uppercase tracking-[0.2em]"
            data-testid={T.rollbackToggleButton}
          >
            <RotateCcw className="w-4 h-4 mr-2" /> Rollback
          </Button>
          <Button
            onClick={startNewRun}
            disabled={submitting}
            className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 text-xs uppercase tracking-[0.2em] font-bold"
            data-testid={T.startRunButton}
          >
            <Play className="w-4 h-4 mr-2" /> Start New Run
          </Button>
        </div>
      </div>

      {rollbackOpen && (
        <div className="panel p-6 space-y-4">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary">// Rollback to previous backup</div>
          {SSHFields()}
          <FormField label="Remote production model path">
            <Input value={fields.remote_production_model_path} onChange={setField("remote_production_model_path")} className={inputClass} placeholder={`${(fields.remote_workdir || "<workdir>").replace(/\/+$/, "")}/models/production.pt (default)`} data-testid={T.remoteProductionModelInput} />
          </FormField>
          <Button
            onClick={submitRollback}
            disabled={submitting}
            className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-10 text-xs uppercase tracking-[0.2em] font-bold"
            data-testid={T.rollbackSubmitButton}
          >
            Confirm Rollback
          </Button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div className="lg:col-span-1">
          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-4 flex items-center gap-2">
            <History className="w-3 h-3" /> Run History ({pipelineData.runs.length})
          </div>
          {pipelineData.runs.length === 0 ? (
            <div className="border border-dashed border-[#27272A] p-10 text-center">
              <Server className="w-8 h-8 text-muted-foreground mx-auto mb-4" strokeWidth={1.5} />
              <p className="text-xs text-muted-foreground">No pipeline runs yet.</p>
            </div>
          ) : (
            <div className="border-t border-[#27272A]">
              {pipelineData.runs.map((run) => (
                <button
                  key={run.id}
                  onClick={() => setSelectedRunId(run.id)}
                  className={`w-full text-left border-b border-[#27272A] p-4 flex items-center justify-between gap-3 transition-colors ${
                    selectedRunId === run.id ? "bg-[#121212] border-l-2 border-l-primary" : "hover:bg-[#1C1C1C]"
                  }`}
                  data-testid={`${T.runRow}-${run.id}`}
                >
                  <div>
                    <div className="font-heading font-medium text-sm">{run.run_type}</div>
                    <div className="text-[10px] text-muted-foreground">{new Date(run.created_at).toLocaleString()}</div>
                  </div>
                  <span className={`text-[9px] uppercase tracking-[0.2em] px-2 py-0.5 font-bold ${
                    TERMINAL_STATUSES.includes(run.status) ? "bg-[#27272A]" : run.status === "failed" ? "bg-destructive text-white" : "bg-primary text-black"
                  }`}>
                    {run.status}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="lg:col-span-2">
          {selectedRun ? (
            <div className="panel p-6 space-y-6">
              {renderStepTracker(selectedRun)}
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-1">
                    // {selectedRun.run_type} run
                  </div>
                  <h3 className="font-heading text-lg font-semibold">{selectedRun.status.replace("_", " ")}</h3>
                </div>
                <Button
                  variant="outline" size="sm"
                  disabled={submitting || selectedRun.busy}
                  onClick={() => deleteRun(selectedRun.id)}
                  className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 h-8 text-xs uppercase tracking-[0.2em]"
                  data-testid={T.deleteRunButton}
                >
                  <Trash2 className="w-3 h-3 mr-1" /> Delete
                </Button>
              </div>
              {renderStepBody(selectedRun)}
            </div>
          ) : (
            <div className="border border-dashed border-[#27272A] p-16 text-center">
              <p className="text-sm text-muted-foreground">Select a run, or start a new one.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
