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

// When nothing needs action any more, still leave the finished image: the next image in the list this
// user can see (the server already limits it by role), wrapping to the first one. null if there is no other.
export function pickFallbackImage(images, currentId) {
  const idx = images.findIndex((i) => i.id === currentId);
  const after = idx >= 0 ? images[idx + 1] : null;
  const hit = after || images.find((i) => i.id !== currentId);
  return hit ? hit.id : null;
}
