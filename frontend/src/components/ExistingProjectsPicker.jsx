import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// Checkbox list of the caller's projects that can be moved into a team
// (projects they own/admin that are not already in `teamId`).
export default function ExistingProjectsPicker({ selected, onChange, teamId, testIdPrefix }) {
  const [projects, setProjects] = useState(null);

  useEffect(() => {
    api.get("/projects")
      .then((r) => setProjects(r.data.filter((p) => p.team_id !== teamId && ["owner", "admin"].includes(p.my_role))))
      .catch(() => setProjects([]));
  }, [teamId]);

  const toggle = (id) => onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);

  if (projects === null) return <div className="text-xs text-muted-foreground">Loading projects...</div>;
  if (!projects.length) return <div className="text-xs text-muted-foreground">You have no other projects to add.</div>;
  return (
    <div className="max-h-56 overflow-y-auto border border-[#27272A]" data-testid={`${testIdPrefix}-existing-projects`}>
      {projects.map((p) => (
        <label key={p.id} className="flex items-center gap-3 p-3 border-b border-[#27272A] last:border-b-0 text-xs cursor-pointer panel-hover">
          <input type="checkbox" className="accent-primary" checked={selected.includes(p.id)} onChange={() => toggle(p.id)}
                 data-testid={`${testIdPrefix}-existing-project-${p.id}`} />
          <span className="flex-1">
            <span className="font-heading font-medium text-sm">{p.name}</span>
            <span className="block text-muted-foreground">{p.team_name || "No team"} · {p.image_count} images</span>
          </span>
        </label>
      ))}
    </div>
  );
}
