import { useNavigate, useOutletContext } from "react-router-dom";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBar, useManagerData, fmt, Loading } from "./shared";
import { MANAGER } from "@/constants/testIds";

export default function Projects() {
  const range = useOutletContext();
  const navigate = useNavigate();
  const { data, loading } = useManagerData("/manager/projects", range);
  if (loading || !data) return <Loading />;
  return (
    <div className="panel overflow-x-auto">
      <Table>
        <TableHeader><TableRow className="border-[#27272A]">
          {["Project", "Team", "Frames (period)", "People", "Annotated", "Review status", "Active model"].map((h) => <TableHead key={h} className="text-[10px] uppercase tracking-[0.2em]">{h}</TableHead>)}
        </TableRow></TableHeader>
        <TableBody>
          {data.projects.map((p) => (
            <TableRow key={p.project_id || p.name} onClick={() => p.project_id && navigate(`/manager/projects/${p.project_id}`)}
              className={`border-[#27272A] ${p.project_id ? "cursor-pointer hover:bg-[#1C1C1C]" : ""}`} data-testid={MANAGER.projectRow(p.name)}>
              <TableCell><div className="font-medium">{p.name}</div>{!p.project_id && <div className="text-[11px] text-muted-foreground">work log only</div>}</TableCell>
              <TableCell className="text-xs">{p.team || "—"}</TableCell>
              <TableCell className="tabular-nums font-bold">{fmt(p.frames_in_range)}</TableCell>
              <TableCell className="tabular-nums">{p.contributors}</TableCell>
              <TableCell className="min-w-[140px]">
                {p.project_id ? (<><div className="text-xs tabular-nums mb-1">{fmt(p.annotated)} / {fmt(p.total_images)} · {p.progress}%</div>
                  <div className="h-1.5 bg-[#1C1C1C]"><div className="h-full bg-primary" style={{ width: `${p.progress}%` }} /></div></>) : "—"}
              </TableCell>
              <TableCell className="min-w-[200px]">{p.project_id ? <StatusBar review={p.review} /> : "—"}</TableCell>
              <TableCell className="text-xs">{p.active_model ? `${p.active_model.name || p.active_model.model_arch}` : "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
