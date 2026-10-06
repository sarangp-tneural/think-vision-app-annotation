import { pickFastAction, queueMode } from "./fastAction";

const base = { readOnly: false, status: "assigned", canReview: false, isAssignedToMe: true, canManageRole: false, assignee: "me" };

test("annotator submits their own open image", () => {
  expect(pickFastAction(base)).toBe("submit");
  expect(pickFastAction({ ...base, status: "rejected" })).toBe("submit");
});

test("annotator has nothing to do on a locked or foreign image", () => {
  expect(pickFastAction({ ...base, readOnly: true })).toBeNull();
  expect(pickFastAction({ ...base, isAssignedToMe: false })).toBeNull();
  expect(pickFastAction({ ...base, status: "approved", readOnly: true })).toBeNull();
});

test("reviewer approves submitted images only", () => {
  const rev = { ...base, canReview: true, readOnly: true, isAssignedToMe: false };
  expect(pickFastAction({ ...rev, status: "submitted" })).toBe("review");
  expect(pickFastAction({ ...rev, status: "assigned" })).toBeNull();
});

test("admin can claim and submit an unassigned image, or review a submitted one", () => {
  const admin = { ...base, canManageRole: true, canReview: true, isAssignedToMe: false, assignee: null, status: "unassigned" };
  expect(pickFastAction(admin)).toBe("submit");
  expect(pickFastAction({ ...admin, status: "submitted", readOnly: true })).toBe("review");
});

test("queue mode follows the action and the reviewer role", () => {
  expect(queueMode("review", "admin")).toBe("review");
  expect(queueMode("submit", "annotator")).toBe("annotate");
  expect(queueMode(null, "reviewer")).toBe("review");
});
