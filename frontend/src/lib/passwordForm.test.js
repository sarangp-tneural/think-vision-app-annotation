import { validatePasswordChange } from "./passwordForm";

const ok = { current: "oldpass123", next: "newpass123", confirm: "newpass123" };

test("a valid change passes", () => {
  expect(validatePasswordChange(ok)).toBeNull();
});

test("each rule reports its own message", () => {
  expect(validatePasswordChange({ ...ok, current: "" })).toMatch(/current password/i);
  expect(validatePasswordChange({ ...ok, next: "short", confirm: "short" })).toMatch(/at least 8/);
  expect(validatePasswordChange({ ...ok, next: "oldpass123", confirm: "oldpass123" })).toMatch(/different/);
  expect(validatePasswordChange({ ...ok, confirm: "other12345" })).toMatch(/do not match/);
});
