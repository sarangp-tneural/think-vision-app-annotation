import { useCallback, useEffect, useState } from "react";
import { api, fileUrl } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DUPLICATES as T } from "@/constants/testIds";
import { Trash2 } from "lucide-react";

export default function DuplicatesDialog({ pid, open, onOpenChange, canDelete, onChanged }) {
  const [data, setData] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get(`/projects/${pid}/duplicates`);
      setData(d);
    } catch (e) {
      toast.error("Could not load duplicates");
    }
  }, [pid]);

  useEffect(() => { if (open) load(); }, [open, load]);

  const remove = async (img) => {
    if (!window.confirm(`Delete "${img.filename}"? The other copies are kept.`)) return;
    try {
      await api.delete(`/images/${img.id}`);
      await load();
      onChanged?.();
    } catch (e) {
      const d = e.response?.data?.detail;
      toast.error(typeof d === "string" ? d : "Delete failed");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-[#121212] border-[#27272A] rounded-sm max-w-3xl max-h-[85vh] overflow-y-auto" data-testid={T.dialog}>
        <DialogHeader>
          <DialogTitle className="font-heading tracking-tight">Duplicate images</DialogTitle>
        </DialogHeader>
        {data && data.groups.length === 0 && (
          <div className="text-xs text-muted-foreground py-4" data-testid={T.empty}>No identical images found.</div>
        )}
        {data && data.unhashed_images > 0 && (
          <div className="text-xs text-muted-foreground" data-testid={T.unhashedNote}>
            {data.unhashed_images} older images have no fingerprint yet, so they can't be compared. An admin can run
            scripts/backfill_content_hash.py to include them.
          </div>
        )}
        <div className="space-y-4">
          {data?.groups.map((g, gi) => (
            <div key={g.content_hash} className="border border-[#27272A] p-3" data-testid={T.group(gi)}>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">{g.count} identical copies</div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {g.images.map((img, i) => (
                  <div key={img.id} className="text-xs space-y-1">
                    <img src={fileUrl(img.storage_path)} alt={img.filename} className="w-full h-24 object-cover border border-[#27272A]" />
                    <div className="truncate" title={img.filename}>{img.filename}</div>
                    <div className="text-muted-foreground">{i === 0 ? "oldest, " : ""}{img.review_status || "unassigned"}{img.source ? ` - ${img.source}` : ""}</div>
                    {canDelete && (
                      <Button size="sm" variant="outline" disabled={img.review_status === "approved"} onClick={() => remove(img)}
                        title={img.review_status === "approved" ? "Approved images can't be deleted" : "Delete this copy"}
                        className="rounded-sm border-[#27272A] bg-transparent h-6 text-[10px] uppercase tracking-[0.2em]" data-testid={T.deleteButton(img.id)}>
                        <Trash2 className="w-3 h-3 mr-1" /> Delete
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
