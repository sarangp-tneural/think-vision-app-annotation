import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { MANAGER } from "@/constants/testIds";

export const STATUS_COLORS = {
  unassigned: "#71717A", assigned: "#06B6D4", submitted: "#EAB308", approved: "#22C55E", rejected: "#EF4444",
};

export const fmt = (n) => (n ?? 0).toLocaleString();
export const pct = (n) => (n == null ? "—" : `${n}%`);
export const shortDate = (iso) => (iso ? iso.slice(5) : "");

export const timeAgo = (iso) => {
  if (!iso) return "never";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${Math.max(mins, 0)}m ago`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
};

export function useManagerData(path, { from, to }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let live = true;
    setLoading(true);
    api.get(path, { params: { from, to } })
      .then((r) => { if (live) setData(r.data); })
      .catch((e) => { if (live) toast.error(e.response?.data?.detail || "Failed to load"); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [path, from, to]);
  return { data, loading };
}

export function Kpi({ label, value, hint, id }) {
  return (
    <div className="panel p-5" data-testid={MANAGER.kpi(id)}>
      <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">{label}</div>
      <div className="font-heading text-3xl font-bold mt-2">{value}</div>
      {hint && <div className="text-[11px] text-muted-foreground mt-1">{hint}</div>}
    </div>
  );
}

export function Section({ title, children, right }) {
  return (
    <section className="panel p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xs uppercase tracking-[0.2em] text-muted-foreground">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

const tooltipStyle = { background: "#121212", border: "1px solid #27272A", borderRadius: 2, fontSize: 12 };

export function DailyChart({ data }) {
  return (
    <div className="h-56" role="img" aria-label="Frames annotated per day">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ left: -16, right: 4 }}>
          <CartesianGrid stroke="#27272A" vertical={false} />
          <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fill: "#A1A1AA", fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={24} />
          <YAxis tick={{ fill: "#A1A1AA", fontSize: 11 }} axisLine={false} tickLine={false} allowDecimals={false} />
          <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "#1C1C1C" }} formatter={(v) => [fmt(v), "Frames"]} labelStyle={{ color: "#FAFAFA" }} />
          <Bar dataKey="frames" fill="#06B6D4" radius={[2, 2, 0, 0]} maxBarSize={28} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function RankBars({ rows, labelKey, onClick }) {
  const max = Math.max(1, ...rows.map((r) => r.frames));
  if (!rows.length) return <Empty />;
  return (
    <ul className="space-y-3">
      {rows.map((r) => (
        <li key={r[labelKey]}>
          <button type="button" disabled={!onClick} onClick={() => onClick?.(r)} className="w-full text-left disabled:cursor-default group">
            <div className="flex justify-between text-xs mb-1">
              <span className="group-enabled:group-hover:text-primary truncate pr-2">{r[labelKey]}</span>
              <span className="text-muted-foreground tabular-nums">{fmt(r.frames)}</span>
            </div>
            <div className="h-1.5 bg-[#1C1C1C]"><div className="h-full bg-primary" style={{ width: `${(r.frames / max) * 100}%` }} /></div>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function StatusBar({ review }) {
  const total = Object.values(review || {}).reduce((a, b) => a + b, 0);
  if (!total) return <span className="text-xs text-muted-foreground">No images</span>;
  return (
    <div>
      <div className="flex h-2 gap-[2px]">
        {Object.entries(review).filter(([, n]) => n > 0).map(([k, n]) => (
          <div key={k} title={`${k}: ${n}`} style={{ width: `${(n / total) * 100}%`, background: STATUS_COLORS[k] || "#71717A" }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[11px] text-muted-foreground">
        {Object.entries(review).filter(([, n]) => n > 0).map(([k, n]) => (
          <span key={k} className="flex items-center gap-1.5">
            <span className="w-2 h-2" style={{ background: STATUS_COLORS[k] || "#71717A" }} />{k} {fmt(n)}
          </span>
        ))}
      </div>
    </div>
  );
}

export const Empty = ({ text = "No activity in this period" }) => (
  <div className="text-xs text-muted-foreground py-6 text-center">{text}</div>
);

export const Loading = () => <div className="text-xs text-muted-foreground py-16 text-center">Loading…</div>;
