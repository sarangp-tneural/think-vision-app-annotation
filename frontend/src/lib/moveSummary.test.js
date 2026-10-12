import { describeMove } from "./moveSummary";

test("the summary names what moves, what resets, and why anything is skipped", () => {
  const results = [
    { id: "a", name: "Alpha", status: "ok", unassigned_images: 2, removed_assignments: 1 },
    { id: "b", name: "Beta", status: "blocked", reason: "Images are waiting for review - finish reviewing first" },
  ];
  const text = describeMove(results, { unassigned_images: 2, removed_assignments: 1 });
  expect(text).toContain("Move 1 project: Alpha.");
  expect(text).toContain("2 image(s)");
  expect(text).toContain("1 project assignment(s)");
  expect(text).toContain('Skipped "Beta"');
});

test("nothing movable means no move or access lines", () => {
  const text = describeMove([{ id: "c", name: "Gamma", status: "skipped" }], { unassigned_images: 0, removed_assignments: 0 });
  expect(text).not.toContain("Move");
  expect(text).not.toContain("lose access");
});
