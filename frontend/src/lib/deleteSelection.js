// How many of the images picked for deletion are approved (deleting those needs an extra confirmation).
export const countApproved = (images, ids) => {
  const picked = new Set(ids);
  return images.filter((i) => picked.has(i.id) && i.review_status === "approved").length;
};
