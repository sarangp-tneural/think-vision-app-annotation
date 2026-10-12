// Plain-language summary of a dry run, shown before projects are moved into a team.
export function describeMove(results, totals) {
  const lines = [];
  const moving = results.filter((r) => r.status === "ok");
  if (moving.length) lines.push(`Move ${moving.length} project${moving.length > 1 ? "s" : ""}: ${moving.map((r) => r.name).join(", ")}.`);
  if (totals.unassigned_images) lines.push(`${totals.unassigned_images} image(s) assigned to people outside this team become unassigned (annotations are kept).`);
  if (totals.removed_assignments) lines.push(`${totals.removed_assignments} project assignment(s) for people outside this team are removed.`);
  if (moving.length) lines.push("Members of the old team who are not in this team lose access to the moved projects.");
  for (const r of results) {
    if (r.status === "blocked" || r.status === "forbidden") lines.push(`Skipped "${r.name || r.id}": ${r.reason}.`);
  }
  return lines.join("\n");
}
