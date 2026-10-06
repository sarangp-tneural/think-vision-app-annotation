// What the one-click "do it and go to the next image" button should do for this user on this image.
// Mirrors the server's workflow rules; the server stays the authority.
//   "review" -> approve/reject, then open the next image waiting for review
//   "submit" -> save + submit for review, then open the next image to annotate
//   null     -> nothing to do here (locked, or not this user's turn)
export function pickFastAction({ readOnly, status, canReview, isAssignedToMe, canManageRole, assignee }) {
  if (canReview && status === "submitted") return "review";
  if (readOnly) return null;
  if (isAssignedToMe && ["assigned", "rejected"].includes(status)) return "submit";
  if (canManageRole && !assignee && status === "unassigned") return "submit"; // saving claims it
  return null;
}

// Which queue "next" should walk for this user.
export const queueMode = (action, myRole) => (action === "review" || myRole === "reviewer" ? "review" : "annotate");
