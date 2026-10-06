import { countApproved } from "./deleteSelection";

const imgs = [
  { id: "a", review_status: "approved" },
  { id: "b", review_status: "assigned" },
  { id: "c", review_status: "approved" },
  { id: "d" },
];

test("counts only approved images among the picked ones", () => {
  expect(countApproved(imgs, ["a", "b", "d"])).toBe(1);
  expect(countApproved(imgs, ["a", "c"])).toBe(2);
  expect(countApproved(imgs, [])).toBe(0);
  expect(countApproved(imgs, ["zzz"])).toBe(0);
});
