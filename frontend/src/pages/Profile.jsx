import { useEffect, useRef, useState } from "react";
import { Camera, Trash2 } from "lucide-react";
import Navbar from "@/components/Navbar";
import UserAvatar from "@/components/UserAvatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, setAuth, updateUser } from "@/lib/api";
import { validatePasswordChange } from "@/lib/passwordForm";
import { toast } from "sonner";
import { PROFILE } from "@/constants/testIds";

const STATS = [
  { key: "teams", label: "Teams" },
  { key: "projects", label: "Projects" },
  { key: "images_assigned", label: "Assigned to me" },
  { key: "images_annotated", label: "Images annotated" },
  { key: "images_reviewed", label: "Images reviewed" },
];
const labelClass = "text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block";
const inputClass = "rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary";
const cardClass = "border border-[#27272A] bg-[#0A0A0A] p-6";
const errMsg = (e, fallback) => e.response?.data?.detail || fallback;

export default function Profile() {
  const [profile, setProfile] = useState(null);
  const [name, setName] = useState("");
  const [pw, setPw] = useState({ current: "", next: "", confirm: "" });
  const [pwError, setPwError] = useState("");
  const [busy, setBusy] = useState("");
  const fileRef = useRef(null);

  useEffect(() => {
    api.get("/profile")
      .then(({ data }) => {
        setProfile(data);
        setName(data.name);
        updateUser({ name: data.name, role: data.role, avatar_path: data.avatar_path });
      })
      .catch(() => toast.error("Failed to load profile"));
  }, []);

  if (!profile) {
    return <div className="min-h-screen bg-background"><Navbar crumbs={[{ label: "Profile" }]} /></div>;
  }

  const applyUser = (u, extra = {}) => {
    setProfile((p) => ({ ...p, ...u, ...extra }));
    updateUser({ name: u.name, avatar_path: u.avatar_path, ...extra });
  };

  const saveName = async (e) => {
    e.preventDefault();
    setBusy("name");
    try {
      const { data } = await api.patch("/profile", { name });
      setName(data.name);
      applyUser(data);
      toast.success("Name updated");
    } catch (err) {
      toast.error(errMsg(err, "Could not update name"));
    } finally { setBusy(""); }
  };

  const changePassword = async (e) => {
    e.preventDefault();
    const problem = validatePasswordChange(pw);
    setPwError(problem || "");
    if (problem) return;
    setBusy("password");
    try {
      const { data } = await api.post("/profile/change-password", { current_password: pw.current, new_password: pw.next });
      setAuth(data.token, { ...JSON.parse(localStorage.getItem("vf_user") || "{}"), ...data.user });
      setPw({ current: "", next: "", confirm: "" });
      toast.success("Password changed. Other devices have been signed out.");
    } catch (err) {
      setPwError(errMsg(err, "Could not change password"));
    } finally { setBusy(""); }
  };

  const uploadAvatar = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) return toast.error("Avatar must be 2 MB or smaller");
    const form = new FormData();
    form.append("file", file);
    setBusy("avatar");
    try {
      const { data } = await api.post("/profile/avatar", form);
      applyUser(data, { avatar_v: Date.now() });
      toast.success("Profile picture updated");
    } catch (err) {
      toast.error(errMsg(err, "Could not upload picture"));
    } finally { setBusy(""); }
  };

  const removeAvatar = async () => {
    setBusy("avatar");
    try {
      const { data } = await api.delete("/profile/avatar");
      applyUser(data);
      toast.success("Profile picture removed");
    } catch (err) {
      toast.error(errMsg(err, "Could not remove picture"));
    } finally { setBusy(""); }
  };

  const setPwField = (k) => (e) => setPw((p) => ({ ...p, [k]: e.target.value }));
  const avatarUser = { ...profile, avatar_v: JSON.parse(localStorage.getItem("vf_user") || "{}").avatar_v };
  const memberSince = profile.created_at ? new Date(profile.created_at).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }) : "-";

  return (
    <div className="min-h-screen bg-background" data-testid={PROFILE.page}>
      <Navbar crumbs={[{ label: "Profile" }]} />
      <main className="max-w-4xl mx-auto px-6 py-10 space-y-6">
        <h1 className="font-heading text-3xl font-bold tracking-tight">Profile</h1>

        <section className={`${cardClass} flex flex-col sm:flex-row sm:items-center gap-6`}>
          <UserAvatar user={avatarUser} className="h-24 w-24 text-2xl" data-testid={PROFILE.avatar} />
          <div className="flex-1 space-y-1">
            <div className="font-heading text-xl font-semibold">{profile.name}</div>
            <div className="text-sm text-muted-foreground" data-testid={PROFILE.email}>{profile.email}</div>
            <div className="text-xs text-muted-foreground">
              <span className="uppercase tracking-[0.2em] text-primary" data-testid={PROFILE.role}>{profile.role}</span>
              {" · member since "}<span data-testid={PROFILE.memberSince}>{memberSince}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={uploadAvatar} data-testid={PROFILE.avatarInput} />
            <Button variant="outline" size="sm" disabled={busy === "avatar"} onClick={() => fileRef.current?.click()}
              className="rounded-sm text-xs uppercase tracking-[0.2em]" data-testid={PROFILE.avatarUploadButton}>
              <Camera className="w-4 h-4 mr-2" /> {profile.avatar_path ? "Change" : "Upload"}
            </Button>
            {profile.avatar_path && (
              <Button variant="ghost" size="sm" disabled={busy === "avatar"} onClick={removeAvatar}
                className="rounded-sm text-xs uppercase tracking-[0.2em] hover:text-red-400" data-testid={PROFILE.avatarRemoveButton}>
                <Trash2 className="w-4 h-4 mr-2" /> Remove
              </Button>
            )}
          </div>
        </section>

        <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {STATS.map(({ key, label }) => (
            <div key={key} className="border border-[#27272A] bg-[#0A0A0A] p-4">
              <div className="font-heading text-2xl font-bold" data-testid={PROFILE.stat(key)}>{profile.stats?.[key] ?? 0}</div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mt-1">{label}</div>
            </div>
          ))}
        </section>

        <form onSubmit={saveName} className={`${cardClass} space-y-4`}>
          <h2 className="font-heading text-lg font-semibold">Account</h2>
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <Label className={labelClass}>Display name</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} className={inputClass} data-testid={PROFILE.nameInput} />
            </div>
            <div>
              <Label className={labelClass}>Email</Label>
              <Input value={profile.email} disabled className={inputClass} />
            </div>
          </div>
          <Button type="submit" disabled={busy === "name" || !name.trim() || name.trim() === profile.name}
            className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold" data-testid={PROFILE.nameSaveButton}>
            Save name
          </Button>
        </form>

        <form onSubmit={changePassword} className={`${cardClass} space-y-4`}>
          <h2 className="font-heading text-lg font-semibold">Change password</h2>
          <div className="grid md:grid-cols-3 gap-4">
            <div>
              <Label className={labelClass}>Current password</Label>
              <Input type="password" autoComplete="current-password" value={pw.current} onChange={setPwField("current")} className={inputClass} data-testid={PROFILE.currentPasswordInput} />
            </div>
            <div>
              <Label className={labelClass}>New password</Label>
              <Input type="password" autoComplete="new-password" value={pw.next} onChange={setPwField("next")} className={inputClass} data-testid={PROFILE.newPasswordInput} />
            </div>
            <div>
              <Label className={labelClass}>Confirm new password</Label>
              <Input type="password" autoComplete="new-password" value={pw.confirm} onChange={setPwField("confirm")} className={inputClass} data-testid={PROFILE.confirmPasswordInput} />
            </div>
          </div>
          {pwError && <p className="text-sm text-red-400" data-testid={PROFILE.passwordError}>{pwError}</p>}
          <p className="text-xs text-muted-foreground">At least 8 characters. Changing your password signs you out on every other device.</p>
          <Button type="submit" disabled={busy === "password"}
            className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold" data-testid={PROFILE.passwordSaveButton}>
            Update password
          </Button>
        </form>
      </main>
    </div>
  );
}
