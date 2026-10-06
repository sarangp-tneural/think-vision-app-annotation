import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { DEPLOY_PIPELINE as T } from "@/constants/testIds";
import { describeEffect, visibleCategories } from "@/lib/mergePlan";
import { AlertTriangle, Loader2 } from "lucide-react";

// Shows what a merge into the server's dataset would do, before anything is written.
export default function MergePlanPreview({ runId, plan, flags, setFlags, building, progress }) {
  const [open, setOpen] = useState(null); // category whose items are listed
  const [rows, setRows] = useState([]);

  const toggle = async (category) => {
    if (open === category) { setOpen(null); return; }
    setOpen(category);
    setRows([]);
    try {
      const { data } = await api.get(`/pipeline/runs/${runId}/merge_plan/items`, { params: { plan_id: plan.id, category, limit: 50 } });
      setRows(data);
    } catch (e) { setRows([]); }
  };

  if (building) {
    return (
      <div className="text-xs text-muted-foreground flex items-center gap-2" data-testid={T.mergePlan}>
        <Loader2 className="w-3 h-3 animate-spin" /> Comparing with the server{progress ? ` - ${progress.phase} ${progress.done}/${progress.total}` : ""}
      </div>
    );
  }
  if (!plan) return null;
  if (plan.status === "failed") {
    return <div className="text-xs text-destructive border border-destructive p-3" data-testid={T.mergePlan}>{plan.error}</div>;
  }
  const c = plan.counts;
  const cats = visibleCategories(c);
  return (
    <div className="border border-[#27272A] p-3 space-y-3 text-xs" data-testid={T.mergePlan}>
      <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
        Preview - nothing has been written to the server yet {plan.status === "expired" && "(expired: preview again)"}
      </div>
      <div className="text-muted-foreground">
        Labels will be written as {{ detect: "boxes", seg: "polygons", obb: "oriented boxes" }[plan.label_format] || plan.label_format}, the format of the server's dataset.
      </div>
      {plan.warnings.map((w) => (
        <div key={w} className="flex gap-2 text-yellow-400" data-testid={T.mergeWarning}><AlertTriangle className="w-4 h-4 shrink-0" /><span>{w}</span></div>
      ))}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {[["To add", c.add], ["Labels edited", c.update_label], ["Already match", c.unchanged], ["Conflicts", c.conflict],
          ["Only on server", c.remote_only], ["Not approved here", c.skipped_rejected]].map(([label, n]) => (
          <div key={label} className="border border-[#27272A] p-2">
            <div className="text-lg font-heading">{n}</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">{label}</div>
          </div>
        ))}
      </div>
      {c.add > 0 && (
        <div className="text-muted-foreground">New images by split: {Object.entries(plan.adds_by_split).map(([k, v]) => `${k} ${v}`).join(" - ")}</div>
      )}
      {cats.map((cat) => (
        <div key={cat.key}>
          <button type="button" onClick={() => toggle(cat.key)} className="underline text-left" data-testid={T.mergeCategory(cat.key)}>
            {cat.label}: {cat.count}
          </button>
          <div className="text-muted-foreground">{cat.help}</div>
          {open === cat.key && (
            <ul className="mt-1 max-h-40 overflow-y-auto font-mono">
              {rows.map((r) => <li key={r.image_id}>{r.filename} {r.split ? `(${r.split})` : ""} - {r.reason}</li>)}
            </ul>
          )}
        </div>
      ))}
      {c.update_label > 0 && (
        <label className="flex items-start gap-2">
          <input type="checkbox" checked={flags.confirm_label_updates} data-testid={T.mergeConfirmLabels}
            onChange={(e) => setFlags((f) => ({ ...f, confirm_label_updates: e.target.checked }))} />
          <span>Replace the {c.update_label} edited label file(s) on the server. The old ones are copied to <code>.thinkvision_backups/</code> first.</span>
        </label>
      )}
      {plan.yaml_change && (
        <div className="border border-yellow-800 p-2 space-y-2">
          <div>data.yaml change: <code>nc {plan.yaml_change.nc_before} to {plan.yaml_change.nc_after}</code>, append{" "}
            {plan.yaml_change.append.map((a) => <code key={a.idx} className="mr-2">{a.idx}: {a.name}</code>)}
          </div>
          <div className="text-muted-foreground">Existing classes are never reordered or renamed. A backup is saved next to it as <code>{plan.yaml_change.backup_path}</code>.</div>
          <label className="flex items-start gap-2">
            <input type="checkbox" checked={flags.update_class_names} data-testid={T.mergeConfirmClasses}
              onChange={(e) => setFlags((f) => ({ ...f, update_class_names: e.target.checked }))} />
            <span>Allow this change to data.yaml (images using these classes are skipped otherwise)</span>
          </label>
        </div>
      )}
      <ul className="list-disc pl-5" data-testid={T.mergeSummary}>
        {describeEffect(plan, flags).map((line) => <li key={line}>{line}</li>)}
      </ul>
    </div>
  );
}
