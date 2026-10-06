import { commitPayload, describeFormat, formatBytes, mappingProblem, withAction } from "./importMapping";

const rows = (...r) => r.map(([server_idx, server_name, action, target]) => ({ server_idx, server_name, action, target }));

test("a default mapping with distinct targets has no problem", () => {
  expect(mappingProblem(rows([0, "person", "new", "person"], [1, "cash", "match", "cash"]), ["cash"], false)).toBeNull();
});

test("two server classes cannot map to the same project class", () => {
  const p = mappingProblem(rows([0, "a", "match", "x"], [1, "b", "match", "x"]), ["x"], false);
  expect(p).toMatch(/both map to "x"/);
});

test("new classes must not already exist or repeat; matches must exist; blanks are rejected", () => {
  expect(mappingProblem(rows([0, "a", "new", "x"]), ["x"], false)).toMatch(/already exists/);
  expect(mappingProblem(rows([0, "a", "new", "n"], [1, "b", "new", "n"]), [], false)).toMatch(/Two server classes/);
  expect(mappingProblem(rows([0, "a", "match", "ghost"]), ["x"], false)).toMatch(/not a project class/);
  expect(mappingProblem(rows([0, "a", "new", "  "]), [], false)).toMatch(/Choose a class/);
});

test("skipped rows are ignored and replace mode skips validation", () => {
  expect(mappingProblem(rows([0, "a", "skip", null], [1, "b", "skip", null]), [], false)).toBeNull();
  expect(mappingProblem(rows([0, "a", "match", "ghost"]), [], true)).toBeNull();
});

test("changing the action picks a sensible default target", () => {
  const row = { server_idx: 0, server_name: "person", action: "new", target: "person" };
  expect(withAction(row, "skip", ["cash"])).toMatchObject({ action: "skip", target: null });
  expect(withAction(row, "match", ["cash", "person"]).target).toBe("person");
  expect(withAction(row, "match", ["cash"]).target).toBe("cash");
  expect(withAction({ ...row, action: "skip", target: null }, "new", []).target).toBe("person");
});

test("commit payload keeps only what the API needs", () => {
  const body = commitPayload([{ server_idx: 0, server_name: "a", action: "new", target: "a" }],
    { replace: false, skipDuplicates: true, includeUnsupported: false, reviewMode: "approved" });
  expect(body).toEqual({
    class_mapping: [{ server_idx: 0, action: "new", target: "a" }],
    replace_project_classes: false, skip_duplicates: true, include_unsupported: false, review_mode: "approved",
  });
});

test("formatBytes", () => {
  expect(formatBytes(0)).toBe("0 B");
  expect(formatBytes(1536)).toBe("1.5 KB");
});

test("describeFormat names the source, the label kind and where the class order came from", () => {
  expect(describeFormat({ source_format: "yolo", label_format: "obb" })).toBe("YOLO, oriented boxes");
  expect(describeFormat({ source_format: "voc", label_format: "voc", names_order: "alphabetical (Pascal VOC has no class list)" }))
    .toBe("Pascal VOC - class order: alphabetical (Pascal VOC has no class list)");
  expect(describeFormat({ source_format: "coco", label_format: "coco", names_order: "category id order" })).toMatch(/^COCO - class order/);
  expect(describeFormat(null)).toBe("");
});
