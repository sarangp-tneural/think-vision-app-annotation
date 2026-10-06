import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { IMPORT_DATASET as T } from "@/constants/testIds";
import { LABEL_FORMATS, SOURCE_FORMATS, commitPayload, describeFormat, formatBytes, mappingProblem, withAction } from "@/lib/importMapping";
import { Loader2, AlertTriangle } from "lucide-react";

const EMPTY_SSH = { host: "", port: 22, username: "", password: "", pem_key: "", remote_workdir: "" };

const RUNNING = ["staging", "scanning", "committing"];
const OUTLINE = "rounded-sm border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]";
const SELECT = "rounded-sm bg-[#0A0A0A] border border-[#27272A] text-xs px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary";
const detail = (e, fallback) => {
  const d = e.response?.data?.detail;
  return typeof d === "string" ? d : d?.message || fallback;
};

export default function ImportDatasetDialog({ pid, open, onOpenChange, project, onChanged }) {
  const [batch, setBatch] = useState(null);
  const [file, setFile] = useState(null);
  const [reviewMode, setReviewMode] = useState("approved");
  const [includeUnsupported, setIncludeUnsupported] = useState(false);
  const [sourceFormat, setSourceFormat] = useState("auto");
  const [labelFormat, setLabelFormat] = useState("auto");
  const [skipDuplicates, setSkipDuplicates] = useState(true);
  const [replace, setReplace] = useState(false);
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState("zip"); // "zip" | "ssh"
  const [ssh, setSsh] = useState(EMPTY_SSH);   // credentials live only in this state: sent per request, never stored
  const [datasets, setDatasets] = useState(null);
  const [yamlPath, setYamlPath] = useState("");
  const fileRef = useRef(null);
  const loadedReport = useRef(null);

  const classes = project?.classes || [];
  const report = batch?.report;

  const refresh = useCallback(async (id) => {
    const { data } = await api.get(`/imports/${id}`);
    setBatch(data);
    return data;
  }, []);

  // On open, pick up an import that is still in flight (or waiting for its mapping to be confirmed).
  useEffect(() => {
    if (!open) return;
    (async () => {
      try {
        const { data } = await api.get(`/projects/${pid}/imports`);
        const active = data.find((b) => b.active);
        if (active) await refresh(active.id);
        else setBatch(null);
      } catch (e) {
        toast.error(detail(e, "Could not load imports"));
      }
    })();
  }, [open, pid, refresh]);

  // Poll while a job is running.
  useEffect(() => {
    if (!open || !batch || !RUNNING.includes(batch.status)) return undefined;
    const t = setInterval(() => {
      refresh(batch.id).then((b) => {
        if (b.status === "completed") onChanged?.();
      }).catch(() => {});
    }, 1000);
    return () => clearInterval(t);
  }, [open, batch, refresh, onChanged]);

  // A fresh dry-run report resets the editable mapping once.
  useEffect(() => {
    if (report && loadedReport.current !== batch.id) {
      loadedReport.current = batch.id;
      setRows(report.class_mapping.map((r) => ({ ...r })));
      setReviewMode(batch.review_mode || "approved");
      setReplace(false);
    }
  }, [report, batch]);

  const problem = report ? mappingProblem(rows, classes, replace) : null;

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("review_mode", reviewMode);
      form.append("include_unsupported", String(includeUnsupported));
      form.append("source_format", sourceFormat);
      form.append("label_format", labelFormat);
      const { data } = await api.post(`/projects/${pid}/imports`, form);
      loadedReport.current = null;
      await refresh(data.batch_id);
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
    } catch (e) {
      toast.error(detail(e, "Upload failed"));
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn, okMsg) => {
    setBusy(true);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
    } catch (e) {
      toast.error(detail(e, "Request failed"));
      if (batch) refresh(batch.id).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const sshCreds = () => ({
    host: ssh.host.trim(), port: Number(ssh.port) || 22, username: ssh.username.trim(),
    password: ssh.password || null, pem_key: ssh.pem_key.trim() || null,
  });

  const findDatasets = () => act(async () => {
    const { data } = await api.post(`/projects/${pid}/imports/ssh/inspect`, { ...sshCreds(), remote_workdir: ssh.remote_workdir.trim() || "~" });
    setDatasets(data.datasets);
    if (data.datasets.length === 0) toast.info("No dataset (data.yaml) found there. Type its path below.");
    else setYamlPath(data.datasets[0].data_yaml_path);
  });

  const importFromServer = () => act(async () => {
    const { data } = await api.post(`/projects/${pid}/imports/ssh`, {
      ...sshCreds(), data_yaml_path: yamlPath.trim(), review_mode: reviewMode, include_unsupported: includeUnsupported,
      label_format: labelFormat,
    });
    loadedReport.current = null;
    setSsh((s) => ({ ...s, password: "", pem_key: "" })); // secrets are dropped as soon as the server has them
    setDatasets(null);
    await refresh(data.batch_id);
  });

  const commit = () => act(async () => {
    await api.post(`/imports/${batch.id}/commit`, commitPayload(rows, { replace, skipDuplicates, includeUnsupported: !!batch.include_unsupported, reviewMode }));
    await refresh(batch.id);
    onChanged?.();
  });
  const resume = () => act(async () => { await api.post(`/imports/${batch.id}/resume`); await refresh(batch.id); });
  const cancel = () => {
    if (!window.confirm("Cancel this import? Images already created are kept.")) return;
    act(async () => { await api.post(`/imports/${batch.id}/cancel`); setBatch(null); loadedReport.current = null; onChanged?.(); }, "Import cancelled");
  };
  const approveAll = () => {
    if (!window.confirm(`Approve all ${batch.pending_count} submitted images from this import?\n\nThey become approved (usable for export and training).`)) return;
    act(async () => {
      const { data } = await api.post(`/imports/${batch.id}/approve-all`, { confirm_count: batch.pending_count });
      await refresh(batch.id);
      onChanged?.();
      toast.success(`Approved ${data.approved} images`);
    });
  };
  const rollback = () => {
    if (!window.confirm("Remove the images from this import that nobody has touched?\nAnything annotated, reopened or assigned is kept.")) return;
    act(async () => {
      const { data } = await api.delete(`/imports/${batch.id}/images`);
      await refresh(batch.id);
      onChanged?.();
      toast.success(`Removed ${data.deleted} images (${data.kept} kept)`);
    });
  };

  const setRow = (idx, next) => setRows((rs) => rs.map((r) => (r.server_idx === idx ? next : r)));
  const pct = batch?.progress?.total ? Math.round((100 * batch.progress.done) / batch.progress.total) : 0;
  const fresh = !batch || ["completed", "failed", "cancelled"].includes(batch.status);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm max-w-3xl max-h-[85vh] overflow-y-auto" data-testid={T.dialog}>
        <DialogHeader>
          <DialogTitle className="font-heading tracking-tight">Import dataset (YOLO detect zip)</DialogTitle>
        </DialogHeader>

        {batch && ["failed", "interrupted", "partial"].includes(batch.status) && batch.error && (
          <div className="flex gap-2 text-xs border border-red-900 bg-red-950/30 text-red-300 p-3" data-testid={T.error}>
            <AlertTriangle className="w-4 h-4 shrink-0" /> <span>{batch.error}</span>
          </div>
        )}

        {fresh && (
          <div className="space-y-4 py-2">
            {batch?.status === "completed" && <CompletedSummary batch={batch} onApprove={approveAll} onRollback={rollback} busy={busy} />}
            <div className="flex gap-2">
              {[["zip", "Zip file", T.sourceZip], ["ssh", "Training server (SSH)", T.sourceSsh]].map(([key, label, tid]) => (
                <Button key={key} size="sm" variant={source === key ? "default" : "outline"} onClick={() => setSource(key)}
                  className={source === key ? "rounded-sm text-[10px] uppercase tracking-[0.2em] h-7" : `${OUTLINE} text-[10px] h-7`} data-testid={tid}>
                  {label}
                </Button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              The dataset needs a data.yaml (its class list defines the class order) and train/valid/test image + label folders.
              Original splits are kept. Nothing is created until you review the report and confirm.
            </p>
            {source === "zip" ? (
              <input ref={fileRef} type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0] || null)}
                className="text-xs block" data-testid={T.fileInput} />
            ) : (
              <SshSourceForm ssh={ssh} setSsh={setSsh} datasets={datasets} yamlPath={yamlPath} setYamlPath={setYamlPath}
                onFind={findDatasets} busy={busy} />
            )}
            <FormatPickers source={source} sourceFormat={sourceFormat} setSourceFormat={setSourceFormat}
              labelFormat={labelFormat} setLabelFormat={setLabelFormat} />
            <ReviewModePicker value={reviewMode} onChange={setReviewMode} />
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={includeUnsupported} onChange={(e) => setIncludeUnsupported(e.target.checked)} data-testid={T.includeUnsupported} />
              Also import images with labels this format can't represent, e.g. COCO masks (they stay unreviewed; those labels are kept untouched)
            </label>
            <DialogFooter>
              {source === "zip" ? (
                <Button onClick={upload} disabled={!file || busy} className="rounded-sm text-xs uppercase tracking-[0.2em]" data-testid={T.uploadButton}>
                  {busy && <Loader2 className="w-4 h-4 mr-2 animate-spin" />} Upload &amp; scan
                </Button>
              ) : (
                <Button onClick={importFromServer} disabled={busy || !ssh.host.trim() || !ssh.username.trim() || !yamlPath.trim()}
                  className="rounded-sm text-xs uppercase tracking-[0.2em]" data-testid={T.sshImportButton}>
                  {busy && <Loader2 className="w-4 h-4 mr-2 animate-spin" />} Copy from server &amp; scan
                </Button>
              )}
            </DialogFooter>
          </div>
        )}

        {batch && RUNNING.includes(batch.status) && (
          <div className="space-y-3 py-4" data-testid={T.progress}>
            <div className="text-xs uppercase tracking-[0.2em] text-muted-foreground" data-testid={T.status}>
              {batch.status === "committing" ? "Importing images" : batch.status === "staging" ? (batch.source_type === "ssh" ? "Copying files from the server" : "Uploading") : "Scanning dataset"}
              {batch.progress?.total ? ` - ${batch.progress.done} / ${batch.progress.total}` : ""}
            </div>
            <Progress value={pct} />
            <Button variant="outline" onClick={cancel} disabled={busy} className={OUTLINE} data-testid={T.cancelButton}>Cancel</Button>
          </div>
        )}

        {batch && ["interrupted", "partial"].includes(batch.status) && (
          <DialogFooter>
            <Button variant="outline" onClick={cancel} disabled={busy} className={OUTLINE} data-testid={T.cancelButton}>Cancel import</Button>
            <Button onClick={resume} disabled={busy} className="rounded-sm text-xs uppercase tracking-[0.2em]" data-testid={T.resumeButton}>Resume</Button>
          </DialogFooter>
        )}

        {batch?.status === "ready" && report && (
          <div className="space-y-5 py-2">
            <Report report={report} />
            <MappingTable rows={rows} classes={classes} replace={replace} onRow={setRow} />
            {report.can_replace_classes && classes.length > 0 && (
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} data-testid={T.replaceClasses} />
                Replace this project's classes with the server's order (no image has annotations yet)
              </label>
            )}
            <ReviewModePicker value={reviewMode} onChange={setReviewMode} />
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={skipDuplicates} onChange={(e) => setSkipDuplicates(e.target.checked)} data-testid={T.skipDuplicates} />
              Skip images already in this project ({(report.duplicates.in_project + report.duplicates.in_batch + report.duplicates.already_imported)} duplicates)
            </label>
            {problem && <div className="text-xs text-red-400" data-testid={T.mappingError}>{problem}</div>}
            <DialogFooter>
              <Button variant="outline" onClick={cancel} disabled={busy} className={OUTLINE} data-testid={T.cancelButton}>Cancel import</Button>
              <Button onClick={commit} disabled={busy || !!problem || (report.importable === 0 && skipDuplicates)}
                className="rounded-sm text-xs uppercase tracking-[0.2em]" data-testid={T.commitButton}>
                {busy && <Loader2 className="w-4 h-4 mr-2 animate-spin" />} Import {report.importable} images
              </Button>
            </DialogFooter>
          </div>
        )}

        {batch?.status === "completed" && (
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)} className="rounded-sm text-xs uppercase tracking-[0.2em]" data-testid={T.closeButton}>Close</Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

function SshSourceForm({ ssh, setSsh, datasets, yamlPath, setYamlPath, onFind, busy }) {
  const set = (k) => (e) => setSsh((s) => ({ ...s, [k]: e.target.value }));
  const INPUT = `${SELECT} w-full`;
  const field = (label, node) => (
    <div className="space-y-1"><Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">{label}</Label>{node}</div>
  );
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
        {field("Host", <input className={INPUT} value={ssh.host} onChange={set("host")} data-testid={T.sshHost} />)}
        {field("Port", <input className={INPUT} type="number" value={ssh.port} onChange={set("port")} data-testid={T.sshPort} />)}
        {field("Username", <input className={INPUT} value={ssh.username} onChange={set("username")} data-testid={T.sshUsername} autoComplete="off" />)}
        {field("Password / passphrase", <input className={INPUT} type="password" value={ssh.password} onChange={set("password")} data-testid={T.sshPassword} autoComplete="new-password" />)}
      </div>
      {field("PEM private key (optional - leave blank for password login)",
        <textarea className={`${INPUT} font-mono h-20`} value={ssh.pem_key} onChange={set("pem_key")} data-testid={T.sshPem}
          placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----\n..."} />)}
      <p className="text-[10px] text-muted-foreground">Credentials are sent once to start the copy. They are never stored, and are cleared here as soon as it starts.</p>
      <div className="flex items-end gap-3">
        <div className="flex-1">{field("Search for datasets under", <input className={INPUT} value={ssh.remote_workdir} onChange={set("remote_workdir")} placeholder="/root/myG" data-testid={T.sshWorkdir} />)}</div>
        <Button variant="outline" size="sm" onClick={onFind} disabled={busy || !ssh.host.trim() || !ssh.username.trim()}
          className={`${OUTLINE} text-[10px] h-8`} data-testid={T.sshFind}>
          {busy && <Loader2 className="w-3 h-3 mr-2 animate-spin" />} Find datasets
        </Button>
      </div>
      {datasets && datasets.length > 0 && (
        <div className="border border-[#27272A]">
          {datasets.map((d, i) => (
            <label key={d.data_yaml_path} className="flex items-start gap-2 px-3 py-2 border-b border-[#27272A] last:border-b-0 text-xs cursor-pointer">
              <input type="radio" name="import-ssh-dataset" checked={yamlPath === d.data_yaml_path}
                onChange={() => setYamlPath(d.data_yaml_path)} data-testid={T.sshDataset(i)} />
              <span>
                <span className="font-mono">{d.data_yaml_path}</span>
                <span className="block text-muted-foreground">
                  {d.names.length} classes - {Object.entries(d.splits).map(([k, v]) => `${k}: ${v.image_count}`).join(", ")}
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
      {field("data.yaml path on the server", <input className={`${INPUT} font-mono`} value={yamlPath} onChange={(e) => setYamlPath(e.target.value)}
        placeholder="/root/myG/myG_combined_v4/data.yaml" data-testid={T.sshYamlPath} />)}
    </div>
  );
}

function FormatPickers({ source, sourceFormat, setSourceFormat, labelFormat, setLabelFormat }) {
  const selectClass = "rounded-sm bg-[#0A0A0A] border border-[#27272A] text-xs px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary";
  const showLabel = source === "ssh" || sourceFormat === "auto" || sourceFormat === "yolo";
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {source === "zip" && (
        <div className="space-y-1">
          <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Dataset format</Label>
          <select className={`${selectClass} w-full`} value={sourceFormat} onChange={(e) => setSourceFormat(e.target.value)} data-testid={T.sourceFormat}>
            {SOURCE_FORMATS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </div>
      )}
      {showLabel && (
        <div className="space-y-1">
          <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">YOLO label format</Label>
          <select className={`${selectClass} w-full`} value={labelFormat} onChange={(e) => setLabelFormat(e.target.value)} data-testid={T.labelFormat}>
            {LABEL_FORMATS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
          <div className="text-[10px] text-muted-foreground">Lines with 9 values can be polygons or oriented boxes, so pick one if asked.</div>
        </div>
      )}
    </div>
  );
}

function ReviewModePicker({ value, onChange }) {
  return (
    <div className="space-y-1">
      <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Review status of imported labels</Label>
      <label className="flex items-center gap-2 text-xs">
        <input type="radio" name="import-review" checked={value === "approved"} onChange={() => onChange("approved")} data-testid={T.reviewApproved} />
        Approved - trust the server's labels (usable for export and training right away)
      </label>
      <label className="flex items-center gap-2 text-xs">
        <input type="radio" name="import-review" checked={value === "submitted"} onChange={() => onChange("submitted")} data-testid={T.reviewSubmitted} />
        Submitted - a reviewer checks them first
      </label>
    </div>
  );
}

function Report({ report }) {
  const stat = (label, value, warn) => (
    <div className={`border p-2 ${warn && value ? "border-yellow-800 bg-yellow-950/20" : "border-[#27272A]"}`}>
      <div className="text-lg font-heading">{value}</div>
      <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">{label}</div>
    </div>
  );
  const split = report.counts_by_split || {};
  return (
    <div className="space-y-3" data-testid={T.report}>
      <div className="text-xs text-muted-foreground">
        {report.origin} - {report.nc} classes from {report.data_yaml} - {formatBytes(report.total_bytes)}
      </div>
      <div className="text-xs" data-testid={T.formatSummary}>{describeFormat(report)}</div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {stat("To import", report.importable)}
        {stat("Train", split.train || 0)}
        {stat("Valid", split.valid || 0)}
        {stat("Test", split.test || 0)}
        {split.unsplit ? stat("No split", split.unsplit) : null}
        {stat("No label file", report.images_without_labels, true)}
        {stat("Corrupt", report.corrupt, true)}
        {stat("Too large", report.too_large, true)}
        {stat("Unsupported labels", report.unsupported_format, true)}
        {report.missing_files ? stat("Listed, not in zip", report.missing_files, true) : null}
        {stat("Duplicates", report.duplicates.in_project + report.duplicates.in_batch + report.duplicates.already_imported, true)}
        {stat("Bad label lines", report.malformed_lines, true)}
      </div>
      {report.unknown_class_indices.length > 0 && (
        <div className="text-xs text-yellow-400">
          Labels use class ids not in data.yaml ({report.unknown_class_indices.map((u) => `#${u.idx}: ${u.boxes}`).join(", ")}); those lines are kept untouched.
        </div>
      )}
      {(report.missing_dirs || []).length > 0 && (
        <div className="text-xs text-yellow-400">
          Folders listed in data.yaml but not found on the server: {report.missing_dirs.join(", ")}
        </div>
      )}
      {report.legacy_unhashed_images > 0 && (
        <div className="text-xs text-muted-foreground">
          {report.legacy_unhashed_images} existing images have no fingerprint yet, so duplicates of those cannot be detected.
        </div>
      )}
    </div>
  );
}

function MappingTable({ rows, classes, replace, onRow }) {
  return (
    <div className="space-y-2">
      <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
        Class mapping - server class to project class (the server's order is kept)
      </Label>
      <div className={`border border-[#27272A] ${replace ? "opacity-40 pointer-events-none" : ""}`}>
        {rows.map((r) => (
            <div key={r.server_idx} className="flex items-center gap-3 px-3 py-1.5 border-b border-[#27272A] last:border-b-0 text-xs" data-testid={T.mappingRow(r.server_idx)}>
              <span className="w-8 text-muted-foreground font-mono">{r.server_idx}</span>
              <span className="w-40 truncate font-mono" title={r.server_name}>{r.server_name}</span>
              <select className={SELECT} value={r.action} data-testid={T.mappingAction(r.server_idx)}
                onChange={(e) => onRow(r.server_idx, withAction(r, e.target.value, classes))}>
                <option value="match" disabled={classes.length === 0}>Existing class</option>
                <option value="new">New class</option>
                <option value="skip">Skip these labels</option>
              </select>
              {r.action === "match" && (
                <select className={SELECT} value={r.target ?? ""} data-testid={T.mappingTarget(r.server_idx)}
                  onChange={(e) => onRow(r.server_idx, { ...r, target: e.target.value })}>
                  {classes.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
              {r.action === "new" && (
                <input className={`${SELECT} w-40`} value={r.target ?? ""} data-testid={T.mappingTarget(r.server_idx)}
                  onChange={(e) => onRow(r.server_idx, { ...r, target: e.target.value })} />
              )}
            </div>
        ))}
      </div>
    </div>
  );
}

function CompletedSummary({ batch, onApprove, onRollback, busy }) {
  const by = batch.counts?.images_by_review_status || {};
  return (
    <div className="border border-[#27272A] p-3 space-y-2 text-xs">
      <div>
        Imported {batch.counts?.items?.imported || 0} images ({Object.entries(by).map(([k, v]) => `${v} ${k}`).join(", ")}).
      </div>
      <div className="flex gap-2">
        {batch.pending_count > 0 && (
          <Button size="sm" onClick={onApprove} disabled={busy} className="rounded-sm text-[10px] uppercase tracking-[0.2em] h-7" data-testid={T.approveAllButton}>
            Approve all {batch.pending_count} submitted
          </Button>
        )}
        <Button size="sm" variant="outline" onClick={onRollback} disabled={busy} className={`${OUTLINE} text-[10px] h-7`} data-testid={T.rollbackButton}>
          Undo import
        </Button>
      </div>
    </div>
  );
}
