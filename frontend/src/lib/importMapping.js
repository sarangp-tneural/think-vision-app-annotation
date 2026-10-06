// Pure helpers for the dataset-import class mapping screen (kept out of the component so they can be tested).

// Server class -> project class mapping rows, as the API returns them: {server_idx, server_name, action, target}.
// action is "match" (existing project class), "new" (appended to the project) or "skip" (labels not imported).

// Returns a user-facing problem with the mapping, or null if it can be committed.
export function mappingProblem(rows, projectClasses, replace) {
  if (replace) return null; // server order replaces the project's classes; the rows are ignored
  const used = new Map();
  const created = new Set();
  for (const r of rows) {
    if (r.action === "skip") continue;
    const target = (r.target ?? "").trim();
    if (!target) return `Choose a class for "${r.server_name}".`;
    if (r.action === "match" && !projectClasses.includes(target)) return `"${target}" is not a project class.`;
    if (r.action === "new") {
      if (projectClasses.includes(target)) return `"${target}" already exists: map "${r.server_name}" to it instead.`;
      if (created.has(target)) return `Two server classes would create "${target}".`;
      created.add(target);
    }
    if (used.has(target)) {
      return `"${used.get(target)}" and "${r.server_name}" both map to "${target}". Each server class needs its own project class.`;
    }
    used.set(target, r.server_name);
  }
  return null;
}

// Changing the action picks a sensible default target.
export function withAction(row, action, projectClasses) {
  if (action === "skip") return { ...row, action, target: null };
  if (action === "match") {
    const same = projectClasses.includes(row.server_name) ? row.server_name : projectClasses[0] ?? "";
    return { ...row, action, target: same };
  }
  return { ...row, action, target: row.server_name };
}

export function commitPayload(rows, { replace, skipDuplicates, includeUnsupported, reviewMode }) {
  return {
    class_mapping: rows.map(({ server_idx, action, target }) => ({ server_idx, action, target })),
    replace_project_classes: !!replace,
    skip_duplicates: skipDuplicates,
    include_unsupported: includeUnsupported,
    review_mode: reviewMode,
  };
}

export const formatBytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
};

export const SOURCE_FORMATS = [
  ["auto", "Detect automatically"], ["yolo", "YOLO (data.yaml)"], ["coco", "COCO (annotation .json)"], ["voc", "Pascal VOC (.xml)"],
];
export const LABEL_FORMATS = [
  ["auto", "Detect automatically"], ["detect", "Boxes (YOLO detect)"], ["seg", "Polygons (YOLO segmentation)"], ["obb", "Oriented boxes (YOLO OBB)"],
];

// One line describing how a dataset is being read, for the dry-run report.
export function describeFormat(report) {
  if (!report) return "";
  const src = { yolo: "YOLO", coco: "COCO", voc: "Pascal VOC" }[report.source_format] || report.source_format;
  const lbl = { detect: "boxes", seg: "polygons", obb: "oriented boxes" }[report.label_format];
  const parts = [lbl ? `${src}, ${lbl}` : src];
  if (report.names_order) parts.push(`class order: ${report.names_order}`);
  return parts.join(" - ");
}
