import { useEffect, useRef, useState } from "react";
import { Upload, Trash2, Plus } from "lucide-react";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { WORK_LOG } from "@/constants/testIds";

const today = () => new Date().toISOString().slice(0, 10);
const blank = () => ({ date: today(), product: "", project_name: "", task: "", frames: "", verified_by: "", notes: "" });
const field = "rounded-sm bg-transparent border-[#27272A] h-9 text-xs";

export default function MyWorkLog() {
  const [rows, setRows] = useState([]);
  const [form, setForm] = useState(blank());
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);

  const load = () => api.get("/work-logs").then((r) => setRows(r.data)).catch(() => toast.error("Failed to load work log"));
  useEffect(() => { load(); }, []);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const add = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/work-logs", { ...form, frames: parseInt(form.frames || "0", 10) });
      setForm((f) => ({ ...blank(), date: f.date }));
      toast.success("Entry added");
      load();
    } catch (err) { toast.error(err.response?.data?.detail || "Could not save"); }
    finally { setBusy(false); }
  };

  const remove = async (id) => {
    if (!window.confirm("Delete this entry?")) return;
    try { await api.delete(`/work-logs/${id}`); load(); } catch { toast.error("Delete failed"); }
  };

  const upload = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      const { data } = await api.post("/work-logs/import", body);
      toast.success(`Imported ${data.imported}, skipped ${data.skipped_duplicates} duplicate(s)`);
      if (data.errors.length) toast.error(`${data.errors.length} row(s) failed, first: row ${data.errors[0].row} – ${data.errors[0].error}`);
      load();
    } catch (err) { toast.error(err.response?.data?.detail || "Import failed"); }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[{ label: "My work log" }]} />
      <main className="max-w-[1400px] mx-auto px-6 py-8 space-y-6">
        <div className="flex items-end justify-between">
          <div>
            <h1 className="font-heading text-3xl font-bold">My work log</h1>
            <p className="text-sm text-muted-foreground mt-1">Frames you annotate in the app are counted automatically. Log other work here, or import your spreadsheet.</p>
          </div>
          <div>
            <input ref={fileRef} type="file" accept=".xlsx,.xlsm,.csv" onChange={upload} className="hidden" data-testid={WORK_LOG.importInput} />
            <Button variant="outline" onClick={() => fileRef.current?.click()} className="rounded-sm border-[#27272A] text-xs"><Upload className="w-4 h-4 mr-2" /> Import .xlsx / .csv</Button>
          </div>
        </div>

        <form onSubmit={add} className="panel p-4 grid grid-cols-2 md:grid-cols-6 gap-3 items-end">
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Date
            <Input type="date" required value={form.date} onChange={set("date")} className={field} data-testid={WORK_LOG.date} /></label>
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Project
            <Input required value={form.project_name} onChange={set("project_name")} className={field} data-testid={WORK_LOG.project} placeholder="MyG" /></label>
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground col-span-2">Task
            <Input value={form.task} onChange={set("task")} className={field} data-testid={WORK_LOG.task} placeholder="Data annotation" /></label>
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Frames
            <Input type="number" min="0" value={form.frames} onChange={set("frames")} className={field} data-testid={WORK_LOG.frames} /></label>
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Verified by
            <Input value={form.verified_by} onChange={set("verified_by")} className={field} data-testid={WORK_LOG.verifiedBy} /></label>
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Product
            <Input value={form.product} onChange={set("product")} className={field} placeholder="Sakshi" /></label>
          <label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground col-span-2 md:col-span-4">Notes
            <Input value={form.notes} onChange={set("notes")} className={field} /></label>
          <Button type="submit" disabled={busy} className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-9 text-xs uppercase tracking-[0.2em] font-bold" data-testid={WORK_LOG.save}>
            <Plus className="w-4 h-4 mr-1" /> Add entry
          </Button>
        </form>

        <div className="panel overflow-x-auto">
          <Table>
            <TableHeader><TableRow className="border-[#27272A]">
              {["Date", "Day", "Product", "Project", "Task", "Frames", "Verified by", ""].map((h) => <TableHead key={h} className="text-[10px] uppercase tracking-[0.2em]">{h}</TableHead>)}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((w) => (
                <TableRow key={w.id} className="border-[#27272A]" data-testid={WORK_LOG.row(w.id)}>
                  <TableCell className="whitespace-nowrap">{w.date}</TableCell><TableCell>{w.day}</TableCell>
                  <TableCell>{w.product}</TableCell><TableCell>{w.project_name}</TableCell>
                  <TableCell className="text-xs max-w-[320px]">{w.task}</TableCell>
                  <TableCell className="tabular-nums font-bold">{w.frames}</TableCell><TableCell>{w.verified_by}</TableCell>
                  <TableCell><button onClick={() => remove(w.id)} aria-label="Delete entry" className="text-muted-foreground hover:text-[#EF4444]" data-testid={WORK_LOG.delete(w.id)}><Trash2 className="w-4 h-4" /></button></TableCell>
                </TableRow>))}
            </TableBody>
          </Table>
          {!rows.length && <div className="text-xs text-muted-foreground py-8 text-center">No entries yet</div>}
        </div>
      </main>
    </div>
  );
}
