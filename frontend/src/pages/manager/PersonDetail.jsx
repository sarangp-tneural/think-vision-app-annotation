import { Link, useOutletContext, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Kpi, Section, DailyChart, RankBars, useManagerData, fmt, pct, timeAgo, Loading, Empty } from "./shared";

export default function PersonDetail() {
  const { uid } = useParams();
  const range = useOutletContext();
  const { data, loading } = useManagerData(`/manager/people/${uid}`, range);
  if (loading || !data) return <Loading />;
  const r = data.review;
  return (
    <div className="space-y-6">
      <div>
        <Link to="/manager/people" className="text-xs text-muted-foreground hover:text-primary flex items-center gap-1 mb-3"><ArrowLeft className="w-3 h-3" /> People</Link>
        <h1 className="font-heading text-3xl font-bold">{data.user.name}</h1>
        <div className="text-xs text-muted-foreground mt-1">{data.user.email} · {data.teams.map((t) => `${t.team} (${t.role})`).join(", ") || "no teams"}</div>
      </div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Kpi id="frames" label="Frames" value={fmt(data.frames)} />
        <Kpi id="days" label="Active days" value={data.active_days} />
        <Kpi id="approved" label="Approved / rejected" value={`${r.approved} / ${r.rejected}`} hint={`approval ${pct(r.approval_rate)}`} />
        <Kpi id="submitted" label="Submitted for review" value={fmt(r.submitted)} />
      </div>
      <Section title="Frames per day"><DailyChart data={data.daily} /></Section>
      <div className="grid lg:grid-cols-2 gap-6">
        <Section title="By project"><RankBars rows={data.by_project} labelKey="project" /></Section>
        <Section title="Recent app activity">
          {data.recent_activity.length ? (
            <ul className="space-y-2 text-xs">
              {data.recent_activity.slice(0, 10).map((a) => (
                <li key={a.id} className="flex justify-between"><span>{a.action.replace(/_/g, " ")}</span><span className="text-muted-foreground">{timeAgo(a.created_at)}</span></li>))}
            </ul>) : <Empty text="No app activity" />}
        </Section>
      </div>
      <Section title="Work log entries">
        {data.work_logs.length ? (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader><TableRow className="border-[#27272A]">
                {["Date", "Product", "Project", "Task", "Frames", "Verified by"].map((h) => <TableHead key={h} className="text-[10px] uppercase tracking-[0.2em]">{h}</TableHead>)}
              </TableRow></TableHeader>
              <TableBody>
                {data.work_logs.map((w) => (
                  <TableRow key={w.id} className="border-[#27272A]">
                    <TableCell className="whitespace-nowrap">{w.date} <span className="text-muted-foreground text-[11px]">{w.day?.slice(0, 3)}</span></TableCell>
                    <TableCell>{w.product}</TableCell><TableCell>{w.project_name}</TableCell>
                    <TableCell className="text-xs max-w-[360px]">{w.task}</TableCell>
                    <TableCell className="tabular-nums">{fmt(w.frames)}</TableCell><TableCell>{w.verified_by}</TableCell>
                  </TableRow>))}
              </TableBody>
            </Table>
          </div>) : <Empty text="No work log entries" />}
      </Section>
    </div>
  );
}
