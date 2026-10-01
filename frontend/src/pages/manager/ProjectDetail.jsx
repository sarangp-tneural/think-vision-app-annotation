import { Link, useOutletContext, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Kpi, Section, DailyChart, StatusBar, useManagerData, fmt, Loading, Empty } from "./shared";

export default function ProjectDetail() {
  const { pid } = useParams();
  const range = useOutletContext();
  const { data, loading } = useManagerData(`/manager/projects/${pid}`, range);
  if (loading || !data) return <Loading />;
  const p = data.project;
  return (
    <div className="space-y-6">
      <div>
        <Link to="/manager/projects" className="text-xs text-muted-foreground hover:text-primary flex items-center gap-1 mb-3"><ArrowLeft className="w-3 h-3" /> Projects</Link>
        <h1 className="font-heading text-3xl font-bold">{p.name}</h1>
        <div className="text-xs text-muted-foreground mt-1">{p.team} · {p.task_type} · {p.classes.length} classes</div>
      </div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Kpi id="total" label="Images" value={fmt(data.total_images)} />
        <Kpi id="annotated" label="Annotated" value={fmt(data.annotated)} hint={`${data.progress}% complete`} />
        <Kpi id="contributors" label="Contributors" value={data.contributors.length} hint="in period" />
        <Kpi id="frames" label="Frames in period" value={fmt(data.daily.reduce((a, d) => a + d.frames, 0))} />
      </div>
      <Section title="Frames per day"><DailyChart data={data.daily} /></Section>
      <Section title="Review pipeline"><StatusBar review={data.review_pipeline} /></Section>
      <Section title="Contributors">
        {data.contributors.length ? (
          <Table>
            <TableHeader><TableRow className="border-[#27272A]">{["Person", "Frames", "Approved", "Rejected"].map((h) => <TableHead key={h} className="text-[10px] uppercase tracking-[0.2em]">{h}</TableHead>)}</TableRow></TableHeader>
            <TableBody>{data.contributors.map((c) => (
              <TableRow key={c.user_id} className="border-[#27272A]">
                <TableCell><Link to={`/manager/people/${c.user_id}`} className="hover:text-primary">{c.name}</Link></TableCell>
                <TableCell className="tabular-nums font-bold">{fmt(c.frames)}</TableCell>
                <TableCell className="tabular-nums text-[#22C55E]">{c.approved}</TableCell>
                <TableCell className="tabular-nums text-[#EF4444]">{c.rejected}</TableCell>
              </TableRow>))}</TableBody>
          </Table>) : <Empty />}
      </Section>
      <div className="grid lg:grid-cols-2 gap-6">
        <Section title="Models">
          {data.models.length ? <ul className="space-y-2 text-xs">{data.models.map((m, i) => (
            <li key={m.name + i} className="flex justify-between"><span>{m.name || m.model_arch}{m.is_active && <span className="text-primary ml-2">active</span>}</span>
              <span className="text-muted-foreground">{m.status}{m.final_mAP != null ? ` · mAP ${Number(m.final_mAP).toFixed(2)}` : ""}</span></li>))}</ul> : <Empty text="No models" />}
        </Section>
        <Section title="Deploy runs">
          {data.deploy_runs.length ? <ul className="space-y-2 text-xs">{data.deploy_runs.map((r) => (
            <li key={r.id} className="flex justify-between"><span>{r.run_type}</span><span className="text-muted-foreground">{r.status} · {r.created_at?.slice(0, 10)}</span></li>))}</ul> : <Empty text="No deploy runs" />}
        </Section>
      </div>
    </div>
  );
}
