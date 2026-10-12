import { api } from "@/lib/api";
import { describeMove } from "@/lib/moveSummary";

// Moves existing projects into a team: dry run, ask for confirmation, then apply.
// Returns the applied results, or null when nothing was moved.
export async function addExistingProjects(tid, projectIds) {
  const preview = (await api.post(`/teams/${tid}/projects/add`, { project_ids: projectIds, dry_run: true })).data;
  if (!preview.results.some((r) => r.status === "ok")) {
    return { results: preview.results, moved: 0 };
  }
  if (!window.confirm(`${describeMove(preview.results, preview.totals)}\n\nContinue?`)) return null;
  const applied = (await api.post(`/teams/${tid}/projects/add`, { project_ids: projectIds })).data;
  return { results: applied.results, moved: applied.results.filter((r) => r.status === "moved").length };
}
