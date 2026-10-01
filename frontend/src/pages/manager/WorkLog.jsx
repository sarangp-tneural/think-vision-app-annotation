import { useOutletContext } from "react-router-dom";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useManagerData, fmt, Loading, Empty } from "./shared";

export default function ManagerWorkLog() {
  const range = useOutletContext();
  const { data, loading } = useManagerData("/manager/work-logs", range);
  if (loading || !data) return <Loading />;
  return (
    <div className="panel overflow-x-auto">
      <Table>
        <TableHeader><TableRow className="border-[#27272A]">
          {["Date", "Day", "Person", "Product", "Project", "Task", "Frames", "Verified by", "Notes"].map((h) => <TableHead key={h} className="text-[10px] uppercase tracking-[0.2em]">{h}</TableHead>)}
        </TableRow></TableHeader>
        <TableBody>
          {data.work_logs.map((w) => (
            <TableRow key={w.id} className="border-[#27272A]">
              <TableCell className="whitespace-nowrap">{w.date}</TableCell><TableCell>{w.day}</TableCell>
              <TableCell>{w.person}</TableCell><TableCell>{w.product}</TableCell><TableCell>{w.project_name}</TableCell>
              <TableCell className="text-xs max-w-[320px]">{w.task}</TableCell>
              <TableCell className="tabular-nums font-bold">{fmt(w.frames)}</TableCell>
              <TableCell>{w.verified_by}</TableCell><TableCell className="text-xs">{w.notes}</TableCell>
            </TableRow>))}
        </TableBody>
      </Table>
      {!data.work_logs.length && <Empty text="No work log entries in this period" />}
    </div>
  );
}
