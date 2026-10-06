// Pure helpers for the merge preview shown before pushing to a server dataset.

export const CATEGORY_INFO = {
  add: { label: "New images to upload", help: "Not on the server yet. They get a split once, and are never moved later." },
  update_label: { label: "Labels edited here", help: "On the server, edited here since the last sync, and the server copy is unchanged. The old label is backed up first. Only written if you tick the box." },
  conflict: { label: "Conflicts (left alone)", help: "The label on the server differs and can't be attributed to your edit. Nothing is written for these." },
  name_collision: { label: "Name already used (left alone)", help: "A different file with the same name is on the server." },
  skipped_empty: { label: "No usable box (not pushed)", help: "Approved, but no box that can be written to a label file." },
};

// Categories worth listing, in display order, with their counts; empty ones are dropped.
export function visibleCategories(counts) {
  return Object.keys(CATEGORY_INFO).filter((k) => (counts?.[k] || 0) > 0).map((k) => ({ key: k, count: counts[k], ...CATEGORY_INFO[k] }));
}

// What pressing Upload will actually do, as sentences for the confirmation step.
export function describeEffect(plan, flags) {
  if (!plan) return ["Only new images will be added. Nothing that is already on the server is changed."];
  const c = plan.counts || {};
  const out = [];
  const needsClass = plan.yaml_change && !flags.update_class_names;
  out.push(`Add ${c.add || 0} new image${c.add === 1 ? "" : "s"}${needsClass ? " (images using a new class are skipped unless you allow the data.yaml change)" : ""}.`);
  if (c.update_label) {
    out.push(flags.confirm_label_updates ? `Replace ${c.update_label} edited label file${c.update_label === 1 ? "" : "s"} (old ones are backed up).` : `Leave ${c.update_label} edited label file(s) as they are on the server.`);
  }
  if (plan.yaml_change) {
    const names = plan.yaml_change.append.map((a) => a.name).join(", ");
    out.push(flags.update_class_names ? `Append class${plan.yaml_change.append.length === 1 ? "" : "es"} to data.yaml: ${names} (backup kept).` : "Leave data.yaml unchanged.");
  }
  out.push(`${c.unchanged || 0} image(s) already match and are not touched.`);
  return out;
}

export function planIsUsable(plan) {
  return !!plan && plan.status === "ready";
}
