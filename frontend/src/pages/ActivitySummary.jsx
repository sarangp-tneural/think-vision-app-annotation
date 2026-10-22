import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";
import { Kpi, Section, StatusBar, Empty, Loading, STATUS_COLORS, fmt, pct, shortDate, timeAgo } from "@/pages/manager/shared";

const RANGES = [7, 30, 90];
const tooltipStyle = { background: "#121212", border: "1px solid #27272A", borderRadius: 2, fontSize: 12 };

const COLUMNS = [
  { key: "name", label: "Person" },
  { key: "assigned", label: "Assigned" },
  { key: "done", label: "Done" },
  { key: "approved", label: "Approved" },
  { key: "rejection_rate", label: "Rejected" },
  { key: "overdue", label: "Overdue" },
  { key: "avg_review_hours", label: "Avg review" },
  { key: "last_active", label: "Last active" },
];

const hours = (h) => (h == null ? "—" : h < 48 ? `${h}h` : `${Math.round(h / 24)}d`);

export default function ActivitySummary({ pid }) {
  const [days, setDays] = useState(30);
  const [data, setData] = useState(null);
  const [sort, setSort] = useState({ key: "assigned", dir: -1 });

  useEffect(() => {
    let live = true;
    api.get(`/projects/${pid}/activity/summary`, { params: { days } })
      .then((r) => { if (live) setData(r.data); })
      .catch((e) => toast.error(e.response?.data?.detail || "Failed to load summary"));
    return () => { live = false; };
  }, [pid, days]);

  const people = useMemo(() => {
    const rows = [...(data?.people || [])];
    const { key, dir } = sort;
    rows.sort((a, b) => {
      const x = a[key] ?? -1, y = b[key] ?? -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
    });
    return rows;
  }, [data, sort]);

  if (!data) return <Loading />;
  const c = data.counts;
  const team = data.scope === "team";

  return (
    <div className="space-y-6" data-testid="activity-summary">
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
        <Kpi id="sum-total" label="Total" value={fmt(data.total)} />
        <Kpi id="sum-unassigned" label="Unassigned" value={fmt(c.unassigned)} />
        <Kpi id="sum-progress" label="In progress" value={fmt(c.assigned)} />
        <Kpi id="sum-submitted" label="Awaiting review" value={fmt(c.submitted)} />
        <Kpi id="sum-approved" label="Approved" value={fmt(c.approved)} />
        <Kpi id="sum-rejected" label="Rejected" value={fmt(c.rejected)} />
        <Kpi id="sum-completion" label="Completion" value={pct(data.completion_pct)} />
      </div>

      <Section title={team ? "Work by person" : "Your work"}>
        {people.length === 0 ? <Empty text="No images are assigned yet" /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs min-w-[840px]" data-testid="summary-people">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                  {COLUMNS.map((col) => (
                    <th key={col.key} className="pb-3 pr-4 font-normal">
                      <button type="button" onClick={() => setSort((s) => ({ key: col.key, dir: s.key === col.key ? -s.dir : -1 }))}
                        className="hover:text-foreground uppercase tracking-[0.2em]">
                        {col.label}{sort.key === col.key ? (sort.dir < 0 ? " ↓" : " ↑") : ""}
                      </button>
                    </th>
                  ))}
                  <th className="pb-3 font-normal w-[28%]">Status</th>
                </tr>
              </thead>
              <tbody>
                {people.map((p) => (
                  <tr key={p.user_id} className="border-t border-[#27272A] align-top" data-testid={`summary-row-${p.user_id}`}>
                    <td className="py-3 pr-4 font-medium">{p.name}</td>
                    <td className="py-3 pr-4 tabular-nums">{fmt(p.assigned)}</td>
                    <td className="py-3 pr-4 tabular-nums">{fmt(p.done)} <span className="text-muted-foreground">({pct(p.done_pct)})</span></td>
                    <td className="py-3 pr-4 tabular-nums">{fmt(p.approved)}</td>
                    <td className="py-3 pr-4 tabular-nums">{pct(p.rejection_rate)}</td>
                    <td className={`py-3 pr-4 tabular-nums ${p.overdue ? "text-destructive font-medium" : "text-muted-foreground"}`}>{fmt(p.overdue)}</td>
                    <td className="py-3 pr-4 tabular-nums">{hours(p.avg_review_hours)}</td>
                    <td className="py-3 pr-4 text-muted-foreground">{timeAgo(p.last_active)}</td>
                    <td className="py-3">
                      <StatusBar review={{ assigned: p.in_progress, submitted: p.submitted, approved: p.approved, rejected: p.rejected }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {data.reviewers.length > 0 && (
        <Section title={team ? "Reviewers" : "Your reviews"}>
          <table className="w-full text-xs" data-testid="summary-reviewers">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                <th className="pb-3 font-normal">Reviewer</th><th className="pb-3 font-normal">Approved</th>
                <th className="pb-3 font-normal">Rejected</th><th className="pb-3 font-normal">Waiting in queue</th>
              </tr>
            </thead>
            <tbody>
              {data.reviewers.map((r) => (
                <tr key={r.user_id} className="border-t border-[#27272A]">
                  <td className="py-3 font-medium">{r.name}</td>
                  <td className="py-3 tabular-nums">{fmt(r.approved)}</td>
                  <td className="py-3 tabular-nums">{fmt(r.rejected)}</td>
                  <td className="py-3 tabular-nums">{fmt(r.queue)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      <Section
        title="Daily activity"
        right={
          <div className="flex gap-1">
            {RANGES.map((d) => (
              <button key={d} type="button" onClick={() => setDays(d)}
                className={`px-2 h-6 text-[10px] uppercase tracking-[0.2em] border ${days === d ? "border-primary text-primary" : "border-[#27272A] text-muted-foreground"}`}>
                {d}d
              </button>
            ))}
          </div>
        }
      >
        <div className="h-56" role="img" aria-label="Submitted, approved and rejected images per day">
          <ResponsiveContainer>
            <BarChart data={data.daily} margin={{ left: -16, right: 4 }}>
              <CartesianGrid stroke="#27272A" vertical={false} />
              <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fill: "#A1A1AA", fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={24} />
              <YAxis tick={{ fill: "#A1A1AA", fontSize: 11 }} axisLine={false} tickLine={false} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "#1C1C1C" }} labelStyle={{ color: "#FAFAFA" }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="submitted" stackId="a" fill={STATUS_COLORS.submitted} maxBarSize={28} />
              <Bar dataKey="approved" stackId="a" fill={STATUS_COLORS.approved} maxBarSize={28} />
              <Bar dataKey="rejected" stackId="a" fill={STATUS_COLORS.rejected} radius={[2, 2, 0, 0]} maxBarSize={28} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Section>
    </div>
  );
}
