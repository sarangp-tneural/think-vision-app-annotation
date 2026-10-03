import { api } from "@/lib/api";
import { toast } from "sonner";

export const normalizeClassName = (s) => s.trim().toLowerCase().slice(0, 40);

// Renames `from` to `rawTo` on every box in the project. Returns the server response plus
// { from, to }, or null if nothing was changed (cancelled, unchanged, or failed).
export async function renameClass(pid, from, rawTo, classes) {
  const to = normalizeClassName(rawTo);
  if (!to || to === from) return null;
  if (classes.includes(to) && !window.confirm(
    `"${to}" already exists. Merge "${from}" into it?\n\nAll "${from}" boxes become "${to}". ` +
    "A model trained before this may label some classes wrong, so retrain it after merging.")) return null;
  try {
    const { data } = await api.patch(`/projects/${pid}/classes/${encodeURIComponent(from)}`, { new_name: to });
    toast.success(`${data.merged ? "Merged" : "Renamed"} "${from}" to "${to}" on ${data.images_updated} image(s)`);
    return { ...data, from, to };
  } catch (e) {
    toast.error(e.response?.data?.detail || "Rename failed");
    return null;
  }
}
