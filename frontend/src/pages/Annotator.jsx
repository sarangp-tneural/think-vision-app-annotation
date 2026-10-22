import { useEffect, useRef, useState, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, fileUrl, getUser } from "@/lib/api";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { sourceLabel } from "@/lib/autolabel";
import { renameClass } from "@/lib/renameClass";
import { pickFastAction, queueMode, pickFallbackImage } from "@/lib/fastAction";
import { CLASSES } from "@/constants/testIds";
import {
  Save, Sparkles, Trash2, ArrowLeft, ArrowRight, Undo2, Copy,
  Square, Hexagon, Slash, Dot, Circle as CircleIcon, Keyboard,
  ZoomIn, ZoomOut, Maximize2, Crosshair, Send, CheckCircle2, XCircle, UserPlus,
  MessageSquare, Reply, Check, Pencil, RotateCw, ChevronDown, MousePointer2,
} from "lucide-react";
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";

const COLORS = ["#06B6D4", "#D946EF", "#EAB308", "#22C55E", "#EF4444", "#F97316", "#3B82F6"];

const TOOLS = [
  { id: "select", label: "Select", icon: MousePointer2, key: "V" },
  { id: "bbox", label: "Box", icon: Square, key: "B" },
  { id: "polygon", label: "Polygon", icon: Hexagon, key: "P" },
  { id: "polyline", label: "Line", icon: Slash, key: "L" },
  { id: "point", label: "Point", icon: Dot, key: "O" },
  { id: "ellipse", label: "Ellipse", icon: CircleIcon, key: "E" },
];

export default function Annotator() {
  const { pid, imgId } = useParams();
  const navigate = useNavigate();
  const containerRef = useRef(null);
  const viewportRef = useRef(null);
  const imageRef = useRef(null);
  const autoSaveTimer = useRef(null);
  const fastRef = useRef({});
  const repeatRef = useRef(() => {});
  const navRef = useRef({ goPrev: () => {}, goNext: () => {} });
  const repeatedForRef = useRef(null); // image id that already received Repeat Previous
  const lastBoxesRef = useRef(null); // { imgId, boxes } of the image just navigated away from
  const [image, setImage] = useState(null);
  const [images, setImages] = useState([]);
  const [project, setProject] = useState(null);
  const [members, setMembers] = useState([]);
  const [boxes, setBoxes] = useState([]);
  const [activeLabel, setActiveLabel] = useState("object");
  const [newLabel, setNewLabel] = useState("");
  const [renamingClass, setRenamingClass] = useState(null); // { from, value }
  const [activeTool, setActiveTool] = useState("bbox");
  const [drawing, setDrawing] = useState(null);
  const [polyPoints, setPolyPoints] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [guides, setGuides] = useState(() => {
    try { return localStorage.getItem("tv_crosshair") !== "0"; } catch { return true; }
  });
  const toggleGuides = () => setGuides((g) => {
    try { localStorage.setItem("tv_crosshair", g ? "0" : "1"); } catch { /* storage unavailable */ }
    return !g;
  });
  const [selectedIdx, setSelectedIdx] = useState(null);
  const [editing, setEditing] = useState(null); // { mode: 'move'|'resize-nw'|..., idx, orig, startX, startY }
  const [saving, setSaving] = useState(false);
  const [autoLoading, setAutoLoading] = useState(false);
  const [imgDims, setImgDims] = useState({ w: 1, h: 1 });
  const [dirty, setDirty] = useState(false);
  const [lastSaved, setLastSaved] = useState(null);
  // Zoom & pan state
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [panning, setPanning] = useState(null);
  const [spaceDown, setSpaceDown] = useState(false);
  // Holding Alt edits boxes (move/resize/rotate) without leaving the current draw tool.
  const [altDown, setAltDown] = useState(false);
  const selectMode = activeTool === "select" || altDown;
  const [comments, setComments] = useState([]);
  const [newComment, setNewComment] = useState("");
  const [replyTo, setReplyTo] = useState(null);
  const [replyText, setReplyText] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [rejectNote, setRejectNote] = useState("");
  const [fastBusy, setFastBusy] = useState(false);
  const currentUser = getUser();

  // Mirrors the server's rules (_image_edit_check): the server is the authority, this keeps the UI honest.
  const myRole = !project ? null : project.team_id ? (members.find((m) => m.user_id === currentUser?.id)?.role || null) : "owner";
  const canManageRole = myRole === "owner" || myRole === "admin";
  const status = image?.review_status || "unassigned";
  const assignee = image?.assigned_to || null;
  let lockReason = null;
  if (myRole === "reviewer") lockReason = "Reviewers review annotations but can't edit them.";
  else if (status === "submitted") lockReason = "Waiting for review. Use Withdraw to edit to make changes.";
  else if (status === "approved") lockReason = "Approved. This image is locked.";
  else if (canManageRole) { if (assignee && assignee !== currentUser?.id) lockReason = "Assigned to another team member."; }
  else if (myRole && assignee !== currentUser?.id) lockReason = "This image isn't assigned to you.";
  else if (myRole && !["assigned", "rejected"].includes(status)) lockReason = "This image isn't open for annotation.";
  const readOnly = !image || !myRole || !!lockReason;

  // Project, image list and team members don't change between images, so they
  // load once per project; only the image and its comments reload on navigation.
  const loadProject = async () => {
    try {
      const [pRes, listRes] = await Promise.all([
        api.get(`/projects/${pid}`),
        api.get(`/projects/${pid}/images`),
      ]);
      setProject(pRes.data);
      setImages(listRes.data);
      if (pRes.data.classes.length > 0) setActiveLabel((cur) => cur || pRes.data.classes[0]);
      if (pRes.data.team_id) {
        try {
          const t = await api.get(`/teams/${pRes.data.team_id}`);
          setMembers((t.data.members || []).filter((m) => m.status === "active"));
        } catch (err) { console.debug("team members load failed", err); }
      }
    } catch (e) {
      toast.error("Failed to load");
      navigate(`/projects/${pid}`);
    }
  };

  const load = async () => {
    try {
      const [iRes, cRes] = await Promise.all([
        api.get(`/images/${imgId}`),
        api.get(`/images/${imgId}/comments`).catch((err) => { console.debug("comments load failed", err); return null; }),
      ]);
      setImage(iRes.data);
      const anns = (iRes.data.annotations || []).map((a, i) => ({ type: a.type || "bbox", uid: a.uid || `saved-${i}-${Math.random().toString(36).slice(2, 9)}`, ...a }));
      setBoxes(anns);
      setDirty(false);
      if (cRes) setComments(cRes.data);
    } catch (e) {
      toast.error("Failed to load");
      navigate(`/projects/${pid}`);
    }
  };

  useEffect(() => { loadProject(); /* eslint-disable-next-line */ }, [pid]);
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [imgId]);

  const labelColor = (label) => {
    if (!project) return COLORS[0];
    const idx = project.classes.indexOf(label);
    return COLORS[(idx >= 0 ? idx : 0) % COLORS.length];
  };

  const getPos = (e) => {
    // e.clientX/Y → position in the transformed image content, normalized 0-1
    const rect = containerRef.current.getBoundingClientRect();
    // The container is the transformed element, so its rect already includes pan and zoom
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
  };

  // Keep the zoomed image covering the viewport so it can't be panned out of view
  const clampPan = (x, y, z) => {
    const vp = viewportRef.current;
    if (!vp || z <= 1) return { x: 0, y: 0 };
    const W = vp.clientWidth, H = vp.clientHeight;
    return { x: Math.max(W - W * z, Math.min(0, x)), y: Math.max(H - H * z, Math.min(0, y)) };
  };

  const zoomAt = (factor, screenX, screenY) => {
    // Cursor relative to the untransformed viewport (the container's rect already includes pan/zoom)
    const vp = viewportRef.current.getBoundingClientRect();
    const sx = screenX - vp.left - viewportRef.current.clientLeft;
    const sy = screenY - vp.top - viewportRef.current.clientTop;
    const newZoom = Math.max(1, Math.min(8, zoom * factor));
    // Zoom around the cursor: new pan such that (sx-panNew)/newZoom === (sx-pan)/zoom
    const nx = sx - ((sx - pan.x) / zoom) * newZoom;
    const ny = sy - ((sy - pan.y) / zoom) * newZoom;
    setZoom(newZoom);
    setPan(clampPan(nx, ny, newZoom));
  };

  const resetView = () => { setZoom(1); setPan({ x: 0, y: 0 }); };

  const onWheel = (e) => {
    if (!e.ctrlKey && !e.metaKey) return; // only zoom when Ctrl/Cmd held
    e.preventDefault();
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
  };

  const commitBoxes = (next) => {
    if (readOnly) return;
    // Ensure each annotation has a stable uid for React keys
    const withIds = next.map((b) => b.uid ? b : { ...b, uid: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}` });
    setBoxes(withIds);
    setDirty(true);
  };

  // Set the selected bbox's rotation from degrees (normalised to 0-359).
  const rotateSelected = (deg) => {
    const b = boxes[selectedIdx];
    if (!b || (b.type || "bbox") !== "bbox" || !Number.isFinite(deg)) return;
    const next = boxes.slice();
    next[selectedIdx] = { ...b, rotation: (((deg % 360) + 360) % 360) * Math.PI / 180 };
    commitBoxes(next);
  };

  const onMouseDown = (e) => {
    if (e.button !== 0) return;
    // Space+drag or middle mouse = pan
    if (spaceDown || e.button === 1) {
      setPanning({ startX: e.clientX, startY: e.clientY, origX: pan.x, origY: pan.y });
      return;
    }
    if (readOnly) return;
    const p = getPos(e);
    const label = activeLabel || "object";

    // Clicked empty canvas → deselect current bbox
    setSelectedIdx(null);
    if (selectMode) return; // select mode never draws
    if (activeTool === "bbox" || activeTool === "ellipse") {
      setDrawing({ tool: activeTool, startX: p.x, startY: p.y, x: p.x, y: p.y, w: 0, h: 0 });
    } else if (activeTool === "point") {
      commitBoxes([...boxes, { type: "point", label, x: p.x, y: p.y, w: 0, h: 0 }]);
    }
  };

  // Start editing a bbox (move or resize via handle). Called from box or handle mousedown.
  const startEdit = (e, idx, mode) => {
    e.stopPropagation();
    e.preventDefault();
    setSelectedIdx(idx);
    if (readOnly) return;
    const b = boxes[idx];
    if (!b || (b.type && b.type !== "bbox")) return;
    setEditing({
      mode, idx,
      orig: { x: b.x || 0, y: b.y || 0, w: b.w || 0, h: b.h || 0, rotation: b.rotation || 0 },
      startX: e.clientX, startY: e.clientY,
    });
  };

  const onMouseMove = (e) => {
    if (panning) {
      const dx = e.clientX - panning.startX;
      const dy = e.clientY - panning.startY;
      setPan(clampPan(panning.origX + dx, panning.origY + dy, zoom));
      return;
    }
    // Handle bbox edit (move/resize/rotate)
    if (editing) {
      const rect = containerRef.current.getBoundingClientRect();
      const o = editing.orig;
      const m = editing.mode;
      if (m === "rotate") {
        // Compute angle from bbox center to current mouse in normalized coords
        const cxN = o.x + o.w / 2;
        const cyN = o.y + o.h / 2;
        // Convert mouse client → normalized inside container (accounting for zoom/pan)
        const px = (e.clientX - rect.left) / rect.width;
        const py = (e.clientY - rect.top) / rect.height;
        // Aspect-correct so rotation feels natural regardless of image dims
        const dx = (px - cxN) * rect.width;
        const dy = (py - cyN) * rect.height;
        // Angle from north (top of bbox); handle is above center by default
        let rotation = Math.atan2(dx, -dy);
        if (e.shiftKey) { const step = Math.PI / 12; rotation = Math.round(rotation / step) * step; }
        const next = boxes.slice();
        next[editing.idx] = { ...next[editing.idx], rotation };
        commitBoxes(next);
        return;
      }
      const dxN = (e.clientX - editing.startX) / rect.width;
      const dyN = (e.clientY - editing.startY) / rect.height;
      let nx = o.x, ny = o.y, nw = o.w, nh = o.h;
      if (m === "move") { nx = o.x + dxN; ny = o.y + dyN; }
      else if (m === "resize-nw") { nx = o.x + dxN; ny = o.y + dyN; nw = o.w - dxN; nh = o.h - dyN; }
      else if (m === "resize-n") { ny = o.y + dyN; nh = o.h - dyN; }
      else if (m === "resize-ne") { ny = o.y + dyN; nw = o.w + dxN; nh = o.h - dyN; }
      else if (m === "resize-e") { nw = o.w + dxN; }
      else if (m === "resize-se") { nw = o.w + dxN; nh = o.h + dyN; }
      else if (m === "resize-s") { nh = o.h + dyN; }
      else if (m === "resize-sw") { nx = o.x + dxN; nw = o.w - dxN; nh = o.h + dyN; }
      else if (m === "resize-w") { nx = o.x + dxN; nw = o.w - dxN; }
      // Clamp minimums; flip if negative
      const MIN = 0.005;
      if (nw < MIN) { nx = nx + nw - MIN; nw = MIN; }
      if (nh < MIN) { ny = ny + nh - MIN; nh = MIN; }
      // Constrain within [0,1]
      nx = Math.max(0, Math.min(1 - MIN, nx));
      ny = Math.max(0, Math.min(1 - MIN, ny));
      nw = Math.max(MIN, Math.min(1 - nx, nw));
      nh = Math.max(MIN, Math.min(1 - ny, nh));
      const next = boxes.slice();
      next[editing.idx] = { ...next[editing.idx], x: nx, y: ny, w: nw, h: nh };
      commitBoxes(next);
      return;
    }
    const p = getPos(e);
    setCursor(p);
    if (!drawing) return;
    if (drawing.tool === "bbox") {
      const x = Math.min(drawing.startX, p.x);
      const y = Math.min(drawing.startY, p.y);
      const w = Math.abs(p.x - drawing.startX);
      const h = Math.abs(p.y - drawing.startY);
      setDrawing({ ...drawing, x, y, w, h });
    } else if (drawing.tool === "ellipse") {
      const rx = Math.abs(p.x - drawing.startX);
      const ry = Math.abs(p.y - drawing.startY);
      setDrawing({ ...drawing, x: drawing.startX, y: drawing.startY, rx, ry });
    }
  };

  const onMouseUp = () => {
    if (panning) { setPanning(null); return; }
    if (editing) { setEditing(null); return; }
    if (!drawing) return;
    const label = activeLabel || "object";
    if (drawing.tool === "bbox" && drawing.w > 0.005 && drawing.h > 0.005) {
      commitBoxes([...boxes, { type: "bbox", label, x: drawing.x, y: drawing.y, w: drawing.w, h: drawing.h }]);
    } else if (drawing.tool === "ellipse" && drawing.rx > 0.005 && drawing.ry > 0.005) {
      commitBoxes([...boxes, { type: "ellipse", label, x: drawing.x, y: drawing.y, rx: drawing.rx, ry: drawing.ry, w: drawing.rx * 2, h: drawing.ry * 2 }]);
    }
    setDrawing(null);
  };

  // While drawing, keep tracking the pointer outside the canvas (clamped to the image edge)
  // and let a release anywhere finish the shape.
  useEffect(() => {
    if (!drawing) return undefined;
    const outside = (e) => !containerRef.current?.contains(e.target);
    const move = (e) => { if (outside(e)) onMouseMove(e); };
    const up = (e) => { if (outside(e)) onMouseUp(); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  });

  const onCanvasClick = (e) => {
    if (spaceDown || selectMode) return;
    if (activeTool !== "polygon" && activeTool !== "polyline") return;
    if (e.detail === 2) return; // handled by dblclick
    const p = getPos(e);
    setPolyPoints([...polyPoints, [p.x, p.y]]);
  };

  const finishPoly = () => {
    if (polyPoints.length < 2) { setPolyPoints([]); return; }
    const label = activeLabel || "object";
    if (activeTool === "polygon" && polyPoints.length >= 3) {
      commitBoxes([...boxes, { type: "polygon", label, points: polyPoints }]);
    } else if (activeTool === "polyline" && polyPoints.length >= 2) {
      commitBoxes([...boxes, { type: "polyline", label, points: polyPoints }]);
    }
    setPolyPoints([]);
  };

  const cancelDrawing = () => {
    setDrawing(null);
    setPolyPoints([]);
  };

  const deleteBox = (i) => commitBoxes(boxes.filter((_, idx) => idx !== i));
  const undo = () => commitBoxes(boxes.slice(0, -1));
  const changeLabel = (i, newLabel) => {
    if (readOnly) return;
    const next = boxes.slice();
    next[i] = { ...next[i], label: newLabel };
    commitBoxes(next);
  };

  const save = async (isAuto = false) => {
    if (saving || readOnly) return;
    setSaving(true);
    try {
      // Strip client-only fields (uid) before saving
      // eslint-disable-next-line no-unused-vars
      const cleanBoxes = boxes.map(({ uid, ...rest }) => rest);
      const { data } = await api.put(`/images/${imgId}/annotations`, { boxes: cleanBoxes });
      setDirty(false);
      setLastSaved(new Date());
      // Saving an unassigned image as owner/admin claims it (the server does the same).
      setImage((im) => (im && !im.assigned_to ? { ...im, assigned_to: currentUser?.id, review_status: "assigned" } : im));
      if (!isAuto) toast.success(`Saved ${boxes.length} annotations`);
      // The PUT response already carries the (possibly-updated) class list,
      // so no separate GET /projects/{pid} round trip is needed here.
      setProject((p) => (p ? { ...p, classes: data.classes } : p));
    } catch (e) {
      const detail = e.response?.data?.detail;
      if (e.response?.status === 422 && detail?.unknown_labels) {
        toast.error(`Not saved: unknown class ${detail.unknown_labels.map((l) => `"${l}"`).join(", ")}. Classes are locked - add the class first.`);
      } else {
        toast.error("Save failed");
      }
    } finally {
      setSaving(false);
    }
  };

  // Auto-save 2s after last change
  useEffect(() => {
    if (!dirty) return;
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current);
    autoSaveTimer.current = setTimeout(() => save(true), 2000);
    return () => autoSaveTimer.current && clearTimeout(autoSaveTimer.current);
    // eslint-disable-next-line
  }, [boxes, dirty]);

  // Prev/Next only change the imgId route param - this component instance
  // stays mounted, so the load() effect below would otherwise overwrite an
  // edit still sitting in the 2s auto-save debounce window before it fires
  // (the bug: navigating away loses the just-made edit). This captures the
  // CURRENT imgId/boxes by argument, not by reading state after navigate()
  // has already changed it, and fires the save in the background rather
  // than blocking navigation on it - success is silent (matching the
  // existing auto-save convention), failure gets a toast since the user has
  // already moved on and wouldn't otherwise know.
  const flushSaveInBackground = (targetImgId, targetBoxes) => {
    // eslint-disable-next-line no-unused-vars
    const cleanBoxes = targetBoxes.map(({ uid, ...rest }) => rest);
    api.put(`/images/${targetImgId}/annotations`, { boxes: cleanBoxes })
      .then(({ data }) => setProject((p) => (p ? { ...p, classes: data.classes } : p)))
      .catch((e) => {
        const unknown = e.response?.data?.detail?.unknown_labels;
        toast.error(unknown
          ? `Previous image not saved: unknown class ${unknown.map((l) => `"${l}"`).join(", ")} (classes are locked)`
          : "Couldn't save the previous image's edits — go back and re-save");
      });
  };

  const goToImage = (targetId) => {
    if (!targetId) return;
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current);
    lastBoxesRef.current = { imgId, boxes };
    if (dirty) flushSaveInBackground(imgId, boxes);
    navigate(`/projects/${pid}/annotate/${targetId}`);
  };

  // Warn on unsaved-changes navigation
  useEffect(() => {
    const beforeUnload = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const autoLabel = async () => {
    if (readOnly) return;
    setAutoLoading(true);
    try {
      const { data } = await api.post(`/images/${imgId}/auto-label`);
      if (data.boxes && data.boxes.length > 0) {
        const detected = data.boxes.map((b) => ({ type: "bbox", ...b }));
        commitBoxes([...boxes, ...detected]);
        const src = sourceLabel(data.source);
        toast.success(`Detected ${detected.length} objects via ${src}`);
      } else {
        toast.info(`No objects detected (source: ${data.source})`);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Auto-label failed");
    } finally {
      setAutoLoading(false);
    }
  };

  // Keyboard shortcuts
  const onKey = useCallback((e) => {
    if (e.target && ["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
    if (meta && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
    if (meta && e.key === "Enter") { e.preventDefault(); e.shiftKey ? fastRef.current.openReject() : fastRef.current.fastRun(); return; }
    if (!e.shiftKey && !e.altKey && !meta && e.key.toLowerCase() === "r") { e.preventDefault(); repeatRef.current(); return; }
    if (e.key === "]") { fastRef.current.jumpToQueueNext(); return; }
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      // Leave arrows alone inside open menus/lists and mid-gesture.
      if (meta || e.altKey || e.target?.closest?.('[role="menu"],[role="listbox"]')) return;
      if (drawing || editing || polyPoints.length) return;
      e.preventDefault();
      if (e.shiftKey) { if (e.key === "ArrowRight") fastRef.current.fastRun(); return; }
      if (e.key === "ArrowRight") navRef.current.goNext(); else navRef.current.goPrev();
      return;
    }
    if (!meta && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "g") { toggleGuides(); return; }
    if (e.key === "Alt") {
      e.preventDefault(); // keep the browser/OS from focusing its menu bar
      if (!drawing && !polyPoints.length) setAltDown(true);
      return;
    }
    if (e.key === "Escape") { cancelDrawing(); setSelectedIdx(null); return; }
    if (e.key === "Enter") { finishPoly(); return; }
    if (e.key === " ") { e.preventDefault(); setSpaceDown(true); return; }
    if (e.key === "+" || e.key === "=") { zoomAt(1.2, window.innerWidth / 2, window.innerHeight / 2); return; }
    if (e.key === "-") { zoomAt(1 / 1.2, window.innerWidth / 2, window.innerHeight / 2); return; }
    if (e.key === "0") { resetView(); return; }
    if (",.<>".includes(e.key) && !meta) {
      const sel = boxes[selectedIdx];
      if (sel && (sel.type || "bbox") === "bbox") {
        e.preventDefault();
        const deg = (e.key === "," || e.key === "<" ? -1 : 1) * (e.shiftKey ? 15 : 1);
        rotateSelected(((sel.rotation || 0) * 180) / Math.PI + deg);
      }
      return;
    }
    const t = TOOLS.find((tl) => tl.key.toLowerCase() === e.key.toLowerCase());
    if (t) { setActiveTool(t.id); cancelDrawing(); }
    // eslint-disable-next-line
  }, [boxes, selectedIdx, polyPoints, activeTool, zoom, pan, drawing, editing]);

  const onKeyUp = useCallback((e) => {
    if (e.key === " ") setSpaceDown(false);
    if (e.key === "Alt") { setAltDown(false); setSelectedIdx(null); setEditing(null); }
  }, []);

  useEffect(() => {
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKeyUp);
    // Alt-Tab away never delivers the keyup, so drop Alt mode on blur.
    const onBlur = () => { setAltDown(false); setEditing(null); };
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [onKey, onKeyUp]);

  // ---- Review workflow ----
  const isAssignedToMe = image?.assigned_to === currentUser?.id;
  const canReview = project?.team_id ? ["owner", "admin", "reviewer"].includes(myRole) : myRole === "owner";
  const reviewStatus = status;

  const assign = async (userId) => {
    try {
      await api.post(`/images/${imgId}/assign`, { user_id: userId || null });
      toast.success(userId ? "Assigned" : "Unassigned");
      setImage({ ...image, assigned_to: userId, review_status: userId ? "assigned" : "unassigned" });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Assign failed");
    }
  };

  const submitForReview = async () => {
    if (dirty) await save();
    try {
      await api.post(`/images/${imgId}/submit`);
      toast.success("Submitted for review");
      setImage({ ...image, review_status: "submitted" });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Submit failed");
    }
  };

  const withdrawSubmission = async () => {
    try {
      await api.post(`/images/${imgId}/withdraw`);
      toast.success("Pulled back. Edit it and submit again.");
      setImage({ ...image, review_status: "assigned" });
      patchListStatus(imgId, "assigned");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Withdraw failed");
    }
  };

  // Takes an approve/reject back (-> submitted) so it can be decided again; reopens the image if we've moved on.
  const undoReview = async (id) => {
    try {
      await api.post(`/images/${id}/undo-review`);
      patchListStatus(id, "submitted");
      toast.success("Review undone - back in the queue");
      if (id === imgId) setImage((im) => (im ? { ...im, review_status: "submitted", review_notes: "" } : im));
      else navigate(`/projects/${pid}/annotate/${id}`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Undo failed");
    }
  };

  const reviewToast = (decision, id) =>
    toast.success(decision === "approve" ? "Approved" : "Rejected", {
      duration: 8000,
      action: { label: "Undo", onClick: () => undoReview(id) },
    });

  const review = async (decision, notes = "") => {
    try {
      await api.post(`/images/${imgId}/review`, { decision, notes });
      reviewToast(decision, imgId);
      setImage({ ...image, review_status: decision === "approve" ? "approved" : "rejected", review_notes: notes });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Review failed");
    }
  };

  // ---- Fast path: act on this image, then land on the next one that needs the same kind of action ----
  const fastAction = pickFastAction({ readOnly, status, canReview, isAssignedToMe, canManageRole, assignee });
  const mode = queueMode(fastAction, myRole);

  const patchListStatus = (id, review_status) =>
    setImages((list) => list.map((i) => (i.id === id ? { ...i, review_status } : i)));

  // Own queue first. Owners/admins can both annotate and review, so when one queue is empty
  // they continue into the other (e.g. approve the last submitted image -> next image to annotate).
  const nextInQueue = async (after) => {
    const modes = canManageRole ? [mode, mode === "review" ? "annotate" : "review"] : [mode];
    for (const m of modes) {
      const { data } = await api.get(`/projects/${pid}/images/next`, { params: { mode: m, after } });
      if (data.id && data.id !== after) return data.id;
    }
    return null;
  };

  // Opens the next image in the queue; the current one needs no flush because callers have just saved it.
  const openNext = (nextId) => {
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current);
    if (nextId && nextId !== imgId) { navigate(`/projects/${pid}/annotate/${nextId}`); return; }
    // Queue is empty: still leave the finished (now locked) image, for the next one this user can see.
    const fallback = pickFallbackImage(images, imgId);
    if (fallback) {
      toast.info("Nothing left to do - showing the next image");
      navigate(`/projects/${pid}/annotate/${fallback}`);
    } else {
      toast.success("All done - no more images");
      navigate(`/projects/${pid}`);
    }
  };

  // "]" - skip ahead without acting; saves a pending edit in the background first.
  const jumpToQueueNext = async () => {
    try {
      const nextId = await nextInQueue(imgId);
      if (nextId && nextId !== imgId) goToImage(nextId);
      else toast.info("Nothing left in your queue");
    } catch { toast.error("Couldn't find the next image"); }
  };

  const fastSubmit = async () => {
    if (fastBusy || saving || fastAction !== "submit") return;
    if (boxes.length === 0) { toast.error("Add at least one annotation before submitting"); return; }
    setFastBusy(true);
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current);
    try {
      // eslint-disable-next-line no-unused-vars
      const cleanBoxes = boxes.map(({ uid, ...rest }) => rest);
      // Save+submit is one request; the next-image lookup runs alongside it.
      const [{ data }, nextId] = await Promise.all([
        api.put(`/images/${imgId}/annotations`, { boxes: cleanBoxes, submit: true }),
        nextInQueue(imgId).catch(() => null),
      ]);
      setDirty(false);
      setProject((p) => (p ? { ...p, classes: data.classes } : p));
      patchListStatus(imgId, "submitted");
      toast.success("Submitted for review");
      openNext(nextId);
    } catch (e) {
      const detail = e.response?.data?.detail;
      toast.error(detail?.unknown_labels
        ? `Not saved: unknown class ${detail.unknown_labels.map((l) => `"${l}"`).join(", ")}. Classes are locked - add the class first.`
        : (typeof detail === "string" ? detail : "Submit failed"));
    } finally {
      setFastBusy(false);
    }
  };

  const fastReview = async (decision, notes = "") => {
    if (fastBusy || fastAction !== "review") return;
    setFastBusy(true);
    try {
      const [, nextId] = await Promise.all([
        api.post(`/images/${imgId}/review`, { decision, notes }),
        nextInQueue(imgId).catch(() => null),
      ]);
      patchListStatus(imgId, decision === "approve" ? "approved" : "rejected");
      reviewToast(decision, imgId);
      setRejecting(false);
      setRejectNote("");
      openNext(nextId);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Review failed");
    } finally {
      setFastBusy(false);
    }
  };

  const fastRun = () => (fastAction === "submit" ? fastSubmit() : fastAction === "review" ? fastReview("approve") : null);
  // Always the latest closures, so the keyboard handler below never acts on stale state.
  fastRef.current = { fastRun, openReject: () => fastAction === "review" && setRejecting(true), jumpToQueueNext };

  // While working on this image, warm the browser cache with the next one in the queue.
  useEffect(() => {
    if (!image || !fastAction) return undefined;
    let stale = false;
    nextInQueue(imgId)
      .then((nid) => (nid && nid !== imgId ? api.get(`/images/${nid}`) : null))
      .then((r) => { if (r && !stale) { const im = new Image(); im.src = fileUrl(r.data.storage_path); } })
      .catch(() => {});
    return () => { stale = true; };
    // eslint-disable-next-line
  }, [imgId, fastAction]);

  // ---- Comments ----
  const addComment = async () => {
    if (!newComment.trim()) return;
    try {
      await api.post(`/images/${imgId}/comments`, { text: newComment });
      setNewComment("");
      const c = await api.get(`/images/${imgId}/comments`);
      setComments(c.data);
    } catch (e) { toast.error("Comment failed"); }
  };

  const addReply = async (parentId) => {
    if (!replyText.trim()) return;
    try {
      await api.post(`/comments/${parentId}/replies`, { text: replyText });
      setReplyText("");
      setReplyTo(null);
      const c = await api.get(`/images/${imgId}/comments`);
      setComments(c.data);
    } catch (e) { toast.error("Reply failed"); }
  };

  const resolveComment = async (cid) => {
    try {
      await api.post(`/comments/${cid}/resolve`);
      const c = await api.get(`/images/${imgId}/comments`);
      setComments(c.data);
    } catch (e) { toast.error("Failed"); }
  };

  // Group comments by thread
  const topComments = comments.filter((c) => !c.parent_id);
  const repliesByParent = comments.filter((c) => c.parent_id).reduce((acc, c) => {
    (acc[c.parent_id] = acc[c.parent_id] || []).push(c);
    return acc;
  }, {});

  const submitClassRename = async () => {
    if (!renamingClass) return;
    const { from, value } = renamingClass;
    setRenamingClass(null);
    const res = await renameClass(pid, from, value, project.classes);
    if (!res) return;
    setProject((p) => (p ? { ...p, classes: res.classes } : p));
    // The server already relabeled the saved boxes; mirror it on the open canvas without marking it dirty.
    setBoxes((bs) => bs.map((b) => (b.label === from ? { ...b, label: res.to } : b)));
    setActiveLabel((cur) => (cur === from ? res.to : cur));
  };

  const addClass = async () => {
    const label = newLabel.trim();
    if (!label) return;
    try {
      const { data } = await api.post(`/projects/${pid}/classes`, { label });
      setProject({ ...project, classes: data.classes });
      setActiveLabel(label);
      setNewLabel("");
    } catch { toast.error("Failed to add class"); }
  };

  const currentIdx = images.findIndex((i) => i.id === imgId);
  const prev = currentIdx > 0 ? images[currentIdx - 1] : null;
  const next = currentIdx < images.length - 1 ? images[currentIdx + 1] : null;

  // Copy the previous image's boxes onto this one at the same positions.
  const repeatPrevious = async () => {
    if (readOnly || !prev || image?.id !== imgId) return;
    // Once per image: a second press (or a double-click) would stack duplicate copies.
    if (repeatedForRef.current === imgId) { toast.info("Already copied from the previous image"); return; }
    repeatedForRef.current = imgId;
    try {
      let src;
      if (lastBoxesRef.current?.imgId === prev.id) src = lastBoxesRef.current.boxes;
      else src = (await api.get(`/images/${prev.id}`)).data.annotations || [];
      if (!src.length) { repeatedForRef.current = null; toast.info("Previous image has no annotations"); return; }
      // Fresh uids so the copies don't share React keys with the source image.
      const copies = src.map((b) => ({ type: "bbox", ...b, uid: undefined }));
      commitBoxes([...boxes, ...copies]);
      toast.success(`Copied ${copies.length} annotation${copies.length === 1 ? "" : "s"} from previous image`);
    } catch { repeatedForRef.current = null; toast.error("Couldn't load the previous image's annotations"); }
  };
  repeatRef.current = repeatPrevious;
  navRef.current = { goPrev: () => goToImage(prev?.id), goNext: () => goToImage(next?.id) };

  if (!image || !project) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <div className="max-w-[1600px] mx-auto px-6 py-16 text-muted-foreground text-sm">Loading annotator...</div>
      </div>
    );
  }

  return (
    <div className="h-screen overflow-hidden bg-background flex flex-col">
      <Navbar crumbs={[
        { label: "Projects", to: "/dashboard" },
        { label: project.name, to: `/projects/${pid}` },
        { label: "Annotate" },
      ]} />

      {/* Toolbar */}
      <div className="border-b border-[#27272A] bg-[#0a0a0a]">
        <div className="max-w-[1600px] mx-auto px-6 py-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => goToImage(prev?.id)} disabled={!prev} title="Previous image (←)" className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] hover:text-primary" data-testid="prev-image-btn">
              <ArrowLeft className="w-4 h-4 mr-1" /> Prev
            </Button>
            <span className="text-xs text-muted-foreground px-2" data-testid="image-counter">
              {currentIdx + 1} / {images.length}
            </span>
            <Button variant="ghost" size="sm" onClick={() => goToImage(next?.id)} disabled={!next} title="Next image (→)" className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] hover:text-primary" data-testid="next-image-btn">
              Next <ArrowRight className="w-4 h-4 ml-1" />
            </Button>

            <div className="ml-3 pl-3 border-l border-[#27272A] flex items-center gap-1" data-testid="tool-palette">
              {TOOLS.map((t) => {
                const Ic = t.icon;
                const on = activeTool === t.id;
                return (
                  <button
                    key={t.id}
                    onClick={() => { setActiveTool(t.id); cancelDrawing(); }}
                    title={`${t.label} (${t.key})`}
                    className={`h-8 w-8 flex items-center justify-center border transition-colors ${on ? "border-primary bg-[#1C1C1C] text-primary" : "border-[#27272A] text-muted-foreground hover:text-primary hover:bg-[#1C1C1C]"}`}
                    data-testid={`tool-${t.id}-btn`}
                  >
                    <Ic className="w-4 h-4" />
                  </button>
                );
              })}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground" data-testid="save-status">
              {saving ? "Saving..." : dirty ? "· Unsaved" : lastSaved ? `Saved · ${lastSaved.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}
            </span>
            <div className="flex items-center border border-[#27272A] rounded-sm h-8" data-testid="zoom-controls">
              <button onClick={() => zoomAt(1 / 1.2, window.innerWidth / 2, window.innerHeight / 2)} className="h-8 w-8 flex items-center justify-center text-muted-foreground hover:text-primary" title="Zoom out (-)" data-testid="zoom-out-btn">
                <ZoomOut className="w-4 h-4" />
              </button>
              <button onClick={resetView} className="h-8 px-2 text-[10px] uppercase tracking-[0.2em] text-muted-foreground hover:text-primary border-l border-r border-[#27272A]" title="Reset (0)" data-testid="zoom-reset-btn">
                {Math.round(zoom * 100)}%
              </button>
              <button onClick={() => zoomAt(1.2, window.innerWidth / 2, window.innerHeight / 2)} className="h-8 w-8 flex items-center justify-center text-muted-foreground hover:text-primary" title="Zoom in (+)" data-testid="zoom-in-btn">
                <ZoomIn className="w-4 h-4" />
              </button>
            </div>
            <button
              onClick={toggleGuides}
              className={`h-8 w-8 flex items-center justify-center border rounded-sm ${guides ? "border-primary text-primary" : "border-[#27272A] text-muted-foreground hover:text-primary"}`}
              title="Crosshair guides (G)"
              data-testid="crosshair-toggle-btn"
            >
              <Crosshair className="w-4 h-4" />
            </button>
            <Button variant="ghost" size="sm" onClick={undo} disabled={boxes.length === 0 || readOnly} className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] hover:text-primary" data-testid="undo-btn" title="Undo (⌘Z)">
              <Undo2 className="w-4 h-4 mr-1" /> Undo
            </Button>
            <Button variant="ghost" size="sm" onClick={repeatPrevious} disabled={!prev || readOnly} className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] hover:text-primary" data-testid="repeat-previous-btn" title="Repeat Previous (R)">
              <Copy className="w-4 h-4 mr-1" /> Repeat Previous
            </Button>
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.15em] text-muted-foreground" data-testid="autolabel-provider">
              <span>
                Using: <strong className="text-primary">{project?.active_model ? (project.active_model.name || project.active_model.model_arch || "trained model") : "Gemini"}</strong>
              </span>
              <button
                onClick={() => navigate(`/projects/${pid}/deploy`)}
                className="underline hover:text-primary"
                data-testid="goto-train-model-btn"
              >
                Train Model
              </button>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={autoLabel}
              disabled={autoLoading || readOnly}
              className="rounded-sm h-8 border-[#27272A] bg-transparent hover:bg-[#1C1C1C] hover:border-primary hover:text-primary text-xs uppercase tracking-[0.2em]"
              data-testid="auto-label-btn"
            >
              <Sparkles className="w-4 h-4 mr-1" /> {autoLoading ? "Detecting..." : "Auto-Label"}
            </Button>
            <Button
              size="sm"
              onClick={() => save()}
              disabled={saving || readOnly}
              className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-8 text-xs uppercase tracking-[0.2em] font-bold"
              data-testid="save-btn"
              title="Save (⌘S)"
            >
              <Save className="w-4 h-4 mr-1" /> {saving ? "Saving..." : "Save"}
            </Button>
            {fastAction && (
              <Button
                size="sm"
                onClick={fastRun}
                disabled={fastBusy || saving}
                className="rounded-sm bg-[#22C55E] text-black hover:bg-[#22C55E]/90 h-8 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="fast-next-btn"
                title="Ctrl+Enter"
              >
                {fastAction === "submit" ? <Send className="w-4 h-4 mr-1" /> : <CheckCircle2 className="w-4 h-4 mr-1" />}
                {fastBusy ? "Working..." : fastAction === "submit" ? "Save & Submit → Next" : "Approve → Next"}
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* Three-pane workspace */}
      <div className="flex-1 min-h-0 flex overflow-hidden">
        {/* Left sidebar: class list */}
        <aside className="w-64 border-r border-[#27272A] bg-[#0a0a0a] p-4 flex-shrink-0 overflow-y-auto">
          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3">Active Class</div>
          <div className="space-y-1 mb-6">
            {project.classes.length === 0 && (
              <div className="text-xs text-muted-foreground">No classes yet. Add one below.</div>
            )}
            {project.classes.map((c) => (
              <div key={c} className="flex items-stretch gap-1">
                {renamingClass?.from === c ? (
                  <Input
                    autoFocus
                    value={renamingClass.value}
                    onChange={(e) => setRenamingClass({ from: c, value: e.target.value })}
                    onKeyDown={(e) => { if (e.key === "Enter") submitClassRename(); if (e.key === "Escape") setRenamingClass(null); }}
                    onBlur={() => setRenamingClass(null)}
                    maxLength={40}
                    className="rounded-sm bg-transparent border-primary h-9 text-xs"
                    data-testid={CLASSES.renameInput}
                  />
                ) : (<>
                  <button
                    onClick={() => setActiveLabel(c)}
                    className={`flex-1 min-w-0 text-left px-3 py-2 border flex items-center gap-2 transition-colors ${
                      activeLabel === c ? "border-primary bg-[#1C1C1C]" : "border-[#27272A] hover:bg-[#1C1C1C]"
                    }`}
                    data-testid={`class-btn-${c}`}
                  >
                    <span className="w-3 h-3 flex-shrink-0" style={{ background: labelColor(c) }} />
                    <span className="text-xs truncate">{c}</span>
                  </button>
                  {canManageRole && (
                    <button
                      onClick={() => setRenamingClass({ from: c, value: c })}
                      className="px-2 border border-[#27272A] text-muted-foreground hover:text-primary hover:bg-[#1C1C1C]"
                      title="Rename class (updates every image)"
                      aria-label={`Rename class ${c}`}
                      data-testid={CLASSES.rename(c)}
                    >
                      <Pencil className="w-3 h-3" />
                    </button>
                  )}
                </>)}
              </div>
            ))}
          </div>

          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2">Add Class</div>
          <div className="flex gap-1">
            <Input
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addClass()}
              placeholder="e.g. car"
              className="rounded-sm bg-transparent border-[#27272A] h-8 text-xs focus-visible:ring-primary"
              data-testid="new-class-input"
            />
            <Button size="sm" onClick={addClass} className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-8 px-2 text-xs" data-testid="add-class-btn">
              +
            </Button>
          </div>

          <div className="mt-8 pt-6 border-t border-[#27272A]">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3 flex items-center gap-2">
              <Keyboard className="w-3 h-3" /> Shortcuts
            </div>
            <div className="space-y-1 text-[10px] text-muted-foreground">
              {TOOLS.map((t) => (
                <div key={t.id} className="flex items-center justify-between"><span>{t.label}</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">{t.key}</kbd></div>
              ))}
              <div className="flex items-center justify-between pt-2"><span>Edit boxes while held</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">Alt</kbd></div>
              <div className="flex items-center justify-between"><span>Save</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">⌘S</kbd></div>
              <div className="flex items-center justify-between"><span>Save/Approve → next</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">⌘↵</kbd></div>
              <div className="flex items-center justify-between"><span>Reject → next</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">⌘⇧↵</kbd></div>
              <div className="flex items-center justify-between"><span>Crosshair guides</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">G</kbd></div>
              <div className="flex items-center justify-between"><span>Prev / next image</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">← →</kbd></div>
              <div className="flex items-center justify-between"><span>Save &amp; Submit → Next</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">⇧→</kbd></div>
              <div className="flex items-center justify-between"><span>Next needing work</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">]</kbd></div>
              <div className="flex items-center justify-between"><span>Undo</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">⌘Z</kbd></div>
              <div className="flex items-center justify-between"><span>Repeat previous</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">R</kbd></div>
              <div className="flex items-center justify-between"><span>Rotate box 1° (⇧ 15°)</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">, .</kbd></div>
              <div className="flex items-center justify-between"><span>Finish polygon</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">↵</kbd></div>
              <div className="flex items-center justify-between pt-2"><span>Zoom in / out</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">Ctrl + wheel</kbd></div>
              <div className="flex items-center justify-between"><span>Zoom in / out (keys)</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">+ -</kbd></div>
              <div className="flex items-center justify-between"><span>Reset zoom</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">0</kbd></div>
              <div className="flex items-center justify-between"><span>Pan image</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">Space + drag</kbd></div>
              <div className="flex items-center justify-between"><span>Cancel</span><kbd className="px-1.5 py-0.5 border border-[#27272A] bg-[#121212]">Esc</kbd></div>
            </div>
          </div>
        </aside>

        {/* Center: canvas */}
        <main className="flex-1 bg-[#050505] flex items-start justify-center p-6 overflow-hidden relative">
          <div
            ref={viewportRef}
            className="relative border border-[#27272A] overflow-hidden bg-[#0a0a0a]"
            style={{
              maxWidth: "100%",
              aspectRatio: imgDims.w / imgDims.h,
              // width capped so height stays within the viewport while keeping the exact image aspect (no letterbox)
              width: `min(${imgDims.w}px, 100%, calc((100vh - 200px) * ${imgDims.w / imgDims.h}))`,
              cursor: spaceDown ? (panning ? "grabbing" : "grab") : selectMode ? "default" : "crosshair",
            }}
            onWheel={onWheel}
          >
            <div
              ref={containerRef}
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseUp={onMouseUp}
              onMouseLeave={() => { if (!drawing) onMouseUp(); setCursor(null); }}
              onClick={onCanvasClick}
              onDoubleClick={finishPoly}
              className="absolute inset-0 no-select"
              style={{
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                transformOrigin: "0 0",
              }}
              data-testid="annotation-canvas"
            >
            <img
              ref={imageRef}
              src={fileUrl(image.storage_path)}
              alt={image.filename}
              onLoad={(e) => setImgDims({ w: e.target.naturalWidth, h: e.target.naturalHeight })}
              className="w-full h-full object-contain pointer-events-none block"
              draggable={false}
            />

            {/* SVG overlay */}
            <svg
              className="absolute inset-0 w-full h-full pointer-events-none"
              viewBox="0 0 100 100"
              preserveAspectRatio="none"
            >
              {/* crosshair guides following the pointer (draw tools only) */}
              {guides && cursor && !selectMode && !spaceDown && !readOnly && (
                <g stroke="#FFFFFF" strokeWidth="2" strokeOpacity="0.9" strokeDasharray="6 4" data-testid="crosshair-guides">
                  <line x1={cursor.x * 100} y1="0" x2={cursor.x * 100} y2="100" vectorEffect="non-scaling-stroke" />
                  <line x1="0" y1={cursor.y * 100} x2="100" y2={cursor.y * 100} vectorEffect="non-scaling-stroke" />
                </g>
              )}
              {boxes.map((b, i) => {
                const color = labelColor(b.label);
                if (b.type === "polygon" || b.type === "polyline") {
                  const pts = (b.points || []).map((p) => `${p[0] * 100},${p[1] * 100}`).join(" ");
                  return b.type === "polygon" ? (
                    <polygon key={b.uid || i} points={pts} fill={color} fillOpacity="0.15" stroke={color} strokeWidth="0.3" vectorEffect="non-scaling-stroke" data-testid={`shape-${i}`} />
                  ) : (
                    <polyline key={b.uid || i} points={pts} fill="none" stroke={color} strokeWidth="0.4" vectorEffect="non-scaling-stroke" data-testid={`shape-${i}`} />
                  );
                }
                if (b.type === "point") {
                  return <circle key={b.uid || i} cx={b.x * 100} cy={b.y * 100} r="0.6" fill={color} vectorEffect="non-scaling-stroke" data-testid={`shape-${i}`} />;
                }
                if (b.type === "ellipse") {
                  return <ellipse key={b.uid || i} cx={b.x * 100} cy={b.y * 100} rx={b.rx * 100} ry={b.ry * 100} fill={color} fillOpacity="0.15" stroke={color} strokeWidth="0.3" vectorEffect="non-scaling-stroke" data-testid={`shape-${i}`} />;
                }
                return null; // bbox handled below
              })}

              {/* polygon/polyline in progress */}
              {(activeTool === "polygon" || activeTool === "polyline") && polyPoints.length > 0 && (
                <>
                  <polyline
                    points={polyPoints.map((p) => `${p[0] * 100},${p[1] * 100}`).join(" ") + (cursor ? ` ${cursor.x * 100},${cursor.y * 100}` : "")}
                    fill="none"
                    stroke={labelColor(activeLabel)}
                    strokeWidth="0.4"
                    strokeDasharray="0.8 0.4"
                    vectorEffect="non-scaling-stroke"
                  />
                  {polyPoints.map((p, i) => (
                    <circle key={`poly-${i}-${p[0]}-${p[1]}`} cx={p[0] * 100} cy={p[1] * 100} r="0.5" fill={labelColor(activeLabel)} vectorEffect="non-scaling-stroke" />
                  ))}
                </>
              )}

              {/* ellipse in progress */}
              {drawing && drawing.tool === "ellipse" && (
                <ellipse cx={drawing.x * 100} cy={drawing.y * 100} rx={(drawing.rx || 0) * 100} ry={(drawing.ry || 0) * 100} fill="none" stroke={labelColor(activeLabel)} strokeWidth="0.3" strokeDasharray="0.8 0.4" vectorEffect="non-scaling-stroke" />
              )}
            </svg>

            {/* bbox rendering as HTML divs (labels + delete easier) */}
            {boxes.map((b, i) => {
              if (b.type !== "bbox" && b.type !== undefined) return null;
              const isSelected = selectedIdx === i;
              const handles = [
                { pos: "nw", style: { left: 0, top: 0, transform: "translate(-50%, -50%)", cursor: "nwse-resize" } },
                { pos: "n",  style: { left: "50%", top: 0, transform: "translate(-50%, -50%)", cursor: "ns-resize" } },
                { pos: "ne", style: { right: 0, top: 0, transform: "translate(50%, -50%)", cursor: "nesw-resize" } },
                { pos: "e",  style: { right: 0, top: "50%", transform: "translate(50%, -50%)", cursor: "ew-resize" } },
                { pos: "se", style: { right: 0, bottom: 0, transform: "translate(50%, 50%)", cursor: "nwse-resize" } },
                { pos: "s",  style: { left: "50%", bottom: 0, transform: "translate(-50%, 50%)", cursor: "ns-resize" } },
                { pos: "sw", style: { left: 0, bottom: 0, transform: "translate(-50%, 50%)", cursor: "nesw-resize" } },
                { pos: "w",  style: { left: 0, top: "50%", transform: "translate(-50%, -50%)", cursor: "ew-resize" } },
              ];
              return (
                <div
                  key={b.uid || i}
                  className={`absolute group/box ${isSelected ? "border-2" : "border-2"}`}
                  style={{
                    left: `${(b.x || 0) * 100}%`,
                    top: `${(b.y || 0) * 100}%`,
                    width: `${(b.w || 0) * 100}%`,
                    height: `${(b.h || 0) * 100}%`,
                    borderColor: labelColor(b.label),
                    boxShadow: isSelected ? `0 0 0 1px ${labelColor(b.label)}` : "none",
                    pointerEvents: spaceDown || !selectMode ? "none" : "auto",
                    cursor: "move",
                    transform: b.rotation ? `rotate(${b.rotation}rad)` : undefined,
                    transformOrigin: "center center",
                  }}
                  onMouseDown={(e) => startEdit(e, i, "move")}
                  data-testid={`bbox-${i}`}
                >
                  {selectMode && (
                  <div
                    className="absolute -top-6 left-0 text-[10px] uppercase tracking-wider px-1.5 py-0.5 font-bold flex items-center gap-1 pointer-events-auto"
                    style={{ background: labelColor(b.label), color: "#000", pointerEvents: spaceDown ? "none" : "auto" }}
                    onMouseDown={(e) => e.stopPropagation()}
                  >
                    {readOnly ? <span>{b.label}</span> : (
                      <DropdownMenu onOpenChange={(open) => { if (open) setSelectedIdx(i); }}>
                        <DropdownMenuTrigger
                          className="flex items-center gap-0.5 uppercase tracking-wider font-bold outline-none hover:underline"
                          onMouseDown={(e) => e.stopPropagation()}
                          onClick={(e) => e.stopPropagation()}
                          title="Change class"
                          data-testid={`bbox-label-menu-${i}`}
                        >
                          <span>{b.label}</span>
                          <ChevronDown className="w-2.5 h-2.5" />
                        </DropdownMenuTrigger>
                        <DropdownMenuContent
                          align="start"
                          className="bg-[#121212] border-[#27272A] rounded-sm max-h-72 overflow-y-auto"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <DropdownMenuLabel className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Change class</DropdownMenuLabel>
                          {project.classes.map((c) => (
                            <DropdownMenuItem
                              key={c}
                              onSelect={() => changeLabel(i, c)}
                              className="text-xs gap-2 cursor-pointer"
                              data-testid={`bbox-label-option-${c}`}
                            >
                              <span className="w-2.5 h-2.5 flex-shrink-0" style={{ background: labelColor(c) }} />
                              <span className="flex-1">{c}</span>
                              {c === b.label && <Check className="w-3 h-3" />}
                            </DropdownMenuItem>
                          ))}
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                    {b.confidence != null && project?.settings?.show_confidence !== false && (
                      <span className="opacity-80" data-testid={`confidence-${i}`}>{Math.round(b.confidence * 100)}%</span>
                    )}
                    {b.source && (
                      <span className="opacity-60 text-[8px]">·{b.source === "model" ? "M" : "G"}</span>
                    )}
                    {b.rotation ? (
                      <span className="opacity-60 text-[8px]">·{Math.round((b.rotation * 180 / Math.PI + 360) % 360)}°</span>
                    ) : null}
                    <button
                      onClick={(e) => { e.stopPropagation(); deleteBox(i); setSelectedIdx(null); }}
                      onMouseDown={(e) => e.stopPropagation()}
                      className="opacity-70 hover:opacity-100"
                      data-testid={`delete-bbox-${i}`}
                    >
                      <Trash2 className="w-2.5 h-2.5" />
                    </button>
                  </div>
                  )}
                  {/* Resize handles (only when selected) */}
                  {isSelected && selectMode && handles.map((h) => (
                    <div
                      key={h.pos}
                      className="absolute w-2.5 h-2.5 bg-[#050505] border-2"
                      style={{
                        ...h.style,
                        borderColor: labelColor(b.label),
                        pointerEvents: "auto",
                      }}
                      onMouseDown={(e) => startEdit(e, i, `resize-${h.pos}`)}
                      data-testid={`resize-${h.pos}-${i}`}
                    />
                  ))}
                  {/* Rotate handle above the box (below it when too close to the top edge) */}
                  {isSelected && selectMode && !readOnly && (() => {
                    const below = (b.y || 0) < 0.06;
                    return (
                      <>
                        <div
                          className="absolute w-px"
                          style={{
                            left: "50%",
                            ...(below ? { bottom: -18 } : { top: -18 }),
                            height: 18,
                            background: labelColor(b.label),
                            pointerEvents: "none",
                          }}
                        />
                        <div
                          className="absolute w-5 h-5 rounded-full border-2 bg-[#050505] flex items-center justify-center"
                          style={{
                            left: "50%",
                            ...(below ? { bottom: -38 } : { top: -38 }),
                            transform: "translate(-50%, 0)",
                            borderColor: labelColor(b.label),
                            color: labelColor(b.label),
                            cursor: editing?.mode === "rotate" ? "grabbing" : "grab",
                            pointerEvents: "auto",
                          }}
                          title="Drag to rotate (Shift = snap 15°)"
                          onMouseDown={(e) => startEdit(e, i, "rotate")}
                          data-testid={`rotate-${i}`}
                        >
                          <RotateCw className="w-3 h-3" />
                        </div>
                      </>
                    );
                  })()}
                </div>
              );
            })}

            {/* bbox in-progress */}
            {drawing && drawing.tool === "bbox" && (
              <div
                className="absolute border-2 border-dashed pointer-events-none"
                style={{
                  left: `${drawing.x * 100}%`,
                  top: `${drawing.y * 100}%`,
                  width: `${drawing.w * 100}%`,
                  height: `${drawing.h * 100}%`,
                  borderColor: labelColor(activeLabel),
                }}
              />
            )}

            {/* Tool hint overlay */}
            {(activeTool === "polygon" || activeTool === "polyline") && polyPoints.length > 0 && (
              <div className="absolute bottom-2 left-2 bg-[#050505]/90 border border-[#27272A] text-[10px] uppercase tracking-[0.2em] text-muted-foreground px-2 py-1 pointer-events-none" style={{ transform: `scale(${1 / zoom})`, transformOrigin: "bottom left" }}>
                {polyPoints.length} pts · Double-click or Enter to finish · Esc to cancel
              </div>
            )}
            </div>
            {selectMode && !spaceDown && (
              <div className="absolute top-2 left-2 bg-[#050505]/90 border border-[#27272A] text-[10px] uppercase tracking-[0.2em] text-primary px-2 py-1 pointer-events-none z-10" data-testid="select-mode-badge">
                SELECT MODE
              </div>
            )}
            {spaceDown && (
              <div className="absolute top-2 left-2 bg-[#050505]/90 border border-[#27272A] text-[10px] uppercase tracking-[0.2em] text-primary px-2 py-1 pointer-events-none z-10">
                PAN MODE
              </div>
            )}
          </div>
        </main>

        {/* Right sidebar: review + boxes list */}
        <aside className="w-72 border-l border-[#27272A] bg-[#0a0a0a] p-4 flex-shrink-0 overflow-y-auto">
          {/* Review workflow panel */}
          <div className="mb-6 pb-6 border-b border-[#27272A]" data-testid="review-panel">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3">Review Status</div>
            <div className={`inline-flex items-center gap-2 text-xs px-2 py-1 border mb-3 ${
              reviewStatus === "approved" ? "border-[#22C55E] text-[#22C55E]" :
              reviewStatus === "rejected" ? "border-destructive text-destructive" :
              reviewStatus === "submitted" ? "border-[#EAB308] text-[#EAB308]" :
              reviewStatus === "assigned" ? "border-primary text-primary" :
              "border-[#27272A] text-muted-foreground"
            }`} data-testid={`review-status-${reviewStatus}`}>
              {reviewStatus === "approved" && <CheckCircle2 className="w-3 h-3" />}
              {reviewStatus === "rejected" && <XCircle className="w-3 h-3" />}
              <span className="uppercase tracking-wider">{reviewStatus.replace("_", " ")}</span>
            </div>

            {lockReason && (
              <div className="text-[11px] p-2 border border-[#EAB308]/40 bg-[#EAB308]/10 mb-3 text-[#EAB308]" data-testid="annotator-lock-banner">
                {lockReason}
              </div>
            )}

            {image?.review_notes && (
              <div className="text-[11px] p-2 border border-[#27272A] bg-[#121212] mb-3 text-muted-foreground">
                <div className="text-[9px] uppercase tracking-[0.2em] text-primary mb-1">Reviewer note</div>
                {image.review_notes}
              </div>
            )}

            {/* Assign */}
            {members.length > 0 && !canManageRole && (
              <div className="mb-3 text-xs text-muted-foreground">
                Assigned to: <span className="text-foreground">{members.find((m) => m.user_id === assignee)?.name || "nobody"}</span>
              </div>
            )}
            {members.length > 0 && canManageRole && (
              <div className="mb-3">
                <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-1">Assigned to</div>
                <Select value={image?.assigned_to || "__none__"} onValueChange={(v) => assign(v === "__none__" ? null : v)}>
                  <SelectTrigger className="rounded-sm bg-transparent border-[#27272A] h-8 text-xs" data-testid="assign-select">
                    <SelectValue placeholder="Unassigned" />
                  </SelectTrigger>
                  <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                    <SelectItem value="__none__">Unassigned</SelectItem>
                    {members.map((m) => (
                      <SelectItem key={m.user_id} value={m.user_id}>{m.name} <span className="text-muted-foreground">({m.role})</span></SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Submit / Review actions */}
            <div className="space-y-2">
              {isAssignedToMe && myRole !== "reviewer" && ["assigned", "rejected"].includes(reviewStatus) && (
                <Button
                  size="sm"
                  onClick={submitForReview}
                  className="w-full rounded-sm bg-primary text-black hover:bg-cyan-400 h-8 text-xs uppercase tracking-[0.2em] font-bold"
                  data-testid="submit-for-review-btn"
                >
                  <Send className="w-3 h-3 mr-2" /> Submit for Review
                </Button>
              )}
              {reviewStatus === "submitted" && myRole !== "reviewer" && (isAssignedToMe || canManageRole) && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={withdrawSubmission}
                  className="w-full rounded-sm border-[#27272A] bg-transparent hover:border-primary hover:text-primary h-8 text-xs uppercase tracking-[0.2em]"
                  data-testid="withdraw-btn"
                >
                  <Undo2 className="w-3 h-3 mr-2" /> Withdraw to edit
                </Button>
              )}
              {canReview && reviewStatus === "submitted" && (
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    size="sm"
                    onClick={() => review("approve")}
                    className="rounded-sm bg-[#22C55E] text-black hover:bg-[#22C55E]/90 h-8 text-xs uppercase tracking-[0.2em] font-bold"
                    data-testid="approve-btn"
                  >
                    <CheckCircle2 className="w-3 h-3 mr-1" /> Approve
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => setRejecting(true)}
                    variant="outline"
                    className="rounded-sm border-destructive text-destructive bg-transparent hover:bg-destructive/10 h-8 text-xs uppercase tracking-[0.2em]"
                    data-testid="reject-btn"
                  >
                    <XCircle className="w-3 h-3 mr-1" /> Reject
                  </Button>
                </div>
              )}
              {canReview && (reviewStatus === "approved" || reviewStatus === "rejected") && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => undoReview(imgId)}
                  className="w-full rounded-sm border-[#27272A] bg-transparent hover:border-primary hover:text-primary h-8 text-xs uppercase tracking-[0.2em]"
                  data-testid="undo-review-btn"
                >
                  <Undo2 className="w-3 h-3 mr-2" /> Undo {reviewStatus === "approved" ? "approval" : "rejection"}
                </Button>
              )}
              {rejecting && canReview && reviewStatus === "submitted" && (
                <div className="space-y-2" data-testid="reject-note-box">
                  <textarea
                    autoFocus
                    value={rejectNote}
                    onChange={(e) => setRejectNote(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") { setRejecting(false); }
                      else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fastReview("reject", rejectNote); }
                    }}
                    placeholder="Note for the annotator (optional) · Enter to reject → next"
                    rows={3}
                    className="w-full rounded-sm bg-transparent border border-[#27272A] text-xs p-2 focus:outline-none focus:border-destructive"
                    data-testid="reject-note-input"
                  />
                  <div className="grid grid-cols-2 gap-2">
                    <Button size="sm" variant="outline" onClick={() => setRejecting(false)} className="rounded-sm h-8 text-xs border-[#27272A] bg-transparent">Cancel</Button>
                    <Button size="sm" disabled={fastBusy} onClick={() => fastReview("reject", rejectNote)} className="rounded-sm h-8 text-xs bg-destructive text-white hover:bg-destructive/90" data-testid="reject-confirm-btn">Reject → Next</Button>
                  </div>
                </div>
              )}
            </div>
          </div>

          {boxes[selectedIdx] && (boxes[selectedIdx].type || "bbox") === "bbox" && (
            <div className="mb-6 pb-6 border-b border-[#27272A]" data-testid="rotation-panel">
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 flex items-center gap-2">
                <RotateCw className="w-3 h-3" /> Box rotation
              </div>
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  step="1"
                  disabled={readOnly}
                  value={Math.round((((boxes[selectedIdx].rotation || 0) * 180) / Math.PI + 360) % 360)}
                  onChange={(e) => rotateSelected(parseFloat(e.target.value))}
                  className="rounded-sm bg-transparent border-[#27272A] h-8 text-xs"
                  data-testid="rotation-input"
                />
                <span className="text-xs text-muted-foreground">°</span>
                <Button size="sm" variant="outline" disabled={readOnly} onClick={() => rotateSelected(0)} className="rounded-sm h-8 text-xs border-[#27272A] bg-transparent" data-testid="rotation-reset-btn">
                  Reset
                </Button>
              </div>
              <div className="text-[10px] text-muted-foreground mt-2">Or drag the round handle on the box. Shift snaps to 15°.</div>
            </div>
          )}

          <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3">
            Annotations ({boxes.length})
          </div>
          {boxes.length === 0 && (
            <div className="text-xs text-muted-foreground">
              Pick a tool and draw on the image, or use Auto-Label.
            </div>
          )}
          <div className="space-y-1">
            {boxes.map((b, i) => {
              const t = b.type || "bbox";
              const size = t === "bbox" ? `${Math.round((b.w || 0) * 100)}×${Math.round((b.h || 0) * 100)}` :
                           t === "ellipse" ? `r ${Math.round((b.rx || 0) * 100)}×${Math.round((b.ry || 0) * 100)}` :
                           t === "polygon" || t === "polyline" ? `${b.points?.length || 0}pts` :
                           t === "point" ? "pt" : "";
              return (
                <div
                  key={b.uid || i}
                  onClick={() => t === "bbox" ? setSelectedIdx(i) : null}
                  className={`flex items-center gap-2 p-2 border transition-colors group cursor-pointer ${selectedIdx === i ? "border-primary bg-[#1C1C1C]" : "border-[#27272A] hover:bg-[#1C1C1C]"}`}
                  data-testid={`annotation-item-${i}`}
                >
                  <span className="w-3 h-3 flex-shrink-0" style={{ background: labelColor(b.label) }} />
                  <Select value={b.label} onValueChange={(v) => changeLabel(i, v)}>
                    <SelectTrigger
                      className="h-6 flex-1 min-w-0 text-xs bg-transparent border-0 hover:border-[#27272A] focus:ring-0 focus:ring-offset-0 px-1 py-0"
                      onClick={(e) => e.stopPropagation()}
                      data-testid={`change-label-${i}`}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
                      {project.classes.map((c) => (
                        <SelectItem key={c} value={c}>{c}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <span className="text-muted-foreground text-[10px] uppercase">{t}</span>
                  <span className="text-[10px] text-muted-foreground">{size}</span>
                  <button
                    onClick={(e) => { e.stopPropagation(); deleteBox(i); }}
                    className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive transition-opacity"
                    data-testid={`delete-annotation-${i}`}
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              );
            })}
          </div>

          {/* Comments panel */}
          <div className="mt-8 pt-6 border-t border-[#27272A]" data-testid="comments-panel">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-3 flex items-center gap-2">
              <MessageSquare className="w-3 h-3" /> Comments ({comments.filter((c) => !c.resolved).length})
            </div>

            <div className="space-y-3 mb-3">
              {topComments.map((c) => (
                <div key={c.id} className={`p-2 border ${c.resolved ? "border-[#27272A] opacity-60" : "border-[#27272A] bg-[#121212]"}`} data-testid={`comment-${c.id}`}>
                  <div className="flex items-center justify-between mb-1">
                    <div className="text-[10px] uppercase tracking-[0.2em] text-primary">{c.user_name}</div>
                    <div className="flex items-center gap-1">
                      {c.resolved && <span className="text-[9px] uppercase tracking-[0.2em] text-[#22C55E]">✓ resolved</span>}
                      <span className="text-[9px] text-muted-foreground">
                        {new Date(c.created_at).toLocaleString([], { hour: "2-digit", minute: "2-digit" })}
                      </span>
                    </div>
                  </div>
                  <div className="text-xs">{c.text}</div>
                  {/* Replies */}
                  {(repliesByParent[c.id] || []).map((r) => (
                    <div key={r.id} className="mt-2 ml-3 pl-2 border-l border-[#27272A]" data-testid={`reply-${r.id}`}>
                      <div className="flex items-center justify-between">
                        <div className="text-[10px] uppercase tracking-[0.2em] text-[#D946EF]">{r.user_name}</div>
                        <span className="text-[9px] text-muted-foreground">
                          {new Date(r.created_at).toLocaleString([], { hour: "2-digit", minute: "2-digit" })}
                        </span>
                      </div>
                      <div className="text-xs">{r.text}</div>
                    </div>
                  ))}
                  {/* Reply / Resolve actions */}
                  {!c.resolved && (
                    <div className="mt-2 flex items-center gap-2">
                      {replyTo === c.id ? (
                        <div className="flex-1 flex gap-1">
                          <Input
                            value={replyText}
                            onChange={(e) => setReplyText(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && addReply(c.id)}
                            placeholder="Reply..."
                            className="h-7 text-xs bg-transparent border-[#27272A] rounded-sm focus-visible:ring-primary"
                            data-testid={`reply-input-${c.id}`}
                            autoFocus
                          />
                          <Button size="sm" onClick={() => addReply(c.id)} className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-7 px-2 text-[10px]" data-testid={`reply-submit-${c.id}`}>Send</Button>
                        </div>
                      ) : (
                        <>
                          <button onClick={() => setReplyTo(c.id)} className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground hover:text-primary flex items-center gap-1" data-testid={`reply-btn-${c.id}`}>
                            <Reply className="w-3 h-3" /> Reply
                          </button>
                          <button onClick={() => resolveComment(c.id)} className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground hover:text-[#22C55E] flex items-center gap-1" data-testid={`resolve-btn-${c.id}`}>
                            <Check className="w-3 h-3" /> Resolve
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))}
              {comments.length === 0 && (
                <div className="text-xs text-muted-foreground">No comments yet</div>
              )}
            </div>

            <div className="flex gap-1">
              <Input
                value={newComment}
                onChange={(e) => setNewComment(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addComment()}
                placeholder="Add a comment..."
                className="h-8 text-xs bg-transparent border-[#27272A] rounded-sm focus-visible:ring-primary"
                data-testid="new-comment-input"
              />
              <Button size="sm" onClick={addComment} className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-8 px-2 text-xs" data-testid="add-comment-btn">
                Send
              </Button>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
