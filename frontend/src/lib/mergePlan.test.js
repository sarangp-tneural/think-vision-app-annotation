import { describeEffect, planIsUsable, visibleCategories } from "./mergePlan";

const plan = {
  status: "ready",
  counts: { add: 3, update_label: 2, conflict: 1, unchanged: 40, name_collision: 0, skipped_empty: 0 },
  yaml_change: { append: [{ idx: 16, name: "mop" }], nc_before: 16, nc_after: 17 },
};

test("only non-empty categories are listed, in a fixed order", () => {
  expect(visibleCategories(plan.counts).map((c) => c.key)).toEqual(["add", "update_label", "conflict"]);
  expect(visibleCategories(undefined)).toEqual([]);
});

test("the effect text follows the checkboxes and never promises more than will happen", () => {
  const off = describeEffect(plan, { confirm_label_updates: false, update_class_names: false }).join(" ");
  expect(off).toMatch(/Add 3 new images/);
  expect(off).toMatch(/skipped unless you allow/);
  expect(off).toMatch(/Leave 2 edited label file/);
  expect(off).toMatch(/Leave data.yaml unchanged/);
  const on = describeEffect(plan, { confirm_label_updates: true, update_class_names: true }).join(" ");
  expect(on).toMatch(/Replace 2 edited label files \(old ones are backed up\)/);
  expect(on).toMatch(/Append class to data.yaml: mop/);
  expect(on).toMatch(/40 image\(s\) already match/);
});

test("without a preview only additions are promised", () => {
  expect(describeEffect(null, {})).toEqual(["Only new images will be added. Nothing that is already on the server is changed."]);
});

test("a plan is usable only when ready", () => {
  expect(planIsUsable(plan)).toBe(true);
  expect(planIsUsable({ ...plan, status: "expired" })).toBe(false);
  expect(planIsUsable({ ...plan, status: "building" })).toBe(false);
  expect(planIsUsable(null)).toBe(false);
});
