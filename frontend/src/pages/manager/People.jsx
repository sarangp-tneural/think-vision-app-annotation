import { useMemo, useState } from "react";
import { useNavigate, useOutletContext } from "react-router-dom";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useManagerData, fmt, pct, timeAgo, Loading, Empty } from "./shared";
import { MANAGER } from "@/constants/testIds";

export default function People() {
  const range = useOutletContext();
  const navigate = useNavigate();
  const { data, loading } = useManagerData("/manager/people", range);
  const [q, setQ] = useState("");
  const rows = useMemo(() => (data?.people || []).filter((p) =>
    `${p.name} ${p.email} ${p.projects.join(" ")}`.toLowerCase().includes(q.toLowerCase())), [data, q]);
  if (loading || !data) return <Loading />;
  return (
    <div className="space-y-4">
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search person or project…" className="max-w-sm rounded-sm bg-transparent border-[#27272A]" data-testid={MANAGER.search} />
      <div className="panel overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-[#27272A]">
              {["Person", "Frames", "Active days", "Avg / day", "Projects", "Submitted", "Approved", "Rejected", "Approval", "Last active"].map((h) => (
                <TableHead key={h} className="text-[10px] uppercase tracking-[0.2em]">{h}</TableHead>))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((p) => (
              <TableRow key={p.user_id} onClick={() => navigate(`/manager/people/${p.user_id}`)} className="cursor-pointer border-[#27272A] hover:bg-[#1C1C1C]" data-testid={MANAGER.personRow(p.user_id)}>
                <TableCell><div className="font-medium">{p.name}</div><div className="text-[11px] text-muted-foreground">{p.email}</div></TableCell>
                <TableCell className="tabular-nums font-bold">{fmt(p.frames)}</TableCell>
                <TableCell className="tabular-nums">{p.active_days}</TableCell>
                <TableCell className="tabular-nums">{fmt(p.avg_per_active_day)}</TableCell>
                <TableCell className="text-xs max-w-[220px] truncate" title={p.projects.join(", ")}>{p.projects.join(", ") || "—"}</TableCell>
                <TableCell className="tabular-nums">{p.submitted}</TableCell>
                <TableCell className="tabular-nums text-[#22C55E]">{p.approved}</TableCell>
                <TableCell className="tabular-nums text-[#EF4444]">{p.rejected}</TableCell>
                <TableCell className="tabular-nums">{pct(p.approval_rate)}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{timeAgo(p.last_active)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!rows.length && <Empty text="No people match" />}
      </div>
    </div>
  );
}
