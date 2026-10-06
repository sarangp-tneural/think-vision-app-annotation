import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";
import { REMOTE_TRAIN } from "@/constants/testIds";

const FIELD = "rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary";
const LABEL = "text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block";

// Connection + environment fields for training on a host over SSH. Credentials stay in this form's state only.
export default function RemoteTrainFields({ pid, value, onChange }) {
  const [checking, setChecking] = useState(false);
  const [info, setInfo] = useState(null);
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });

  const checkHost = async () => {
    setChecking(true);
    setInfo(null);
    try {
      const pem = value.authMode === "pem";
      const { data } = await api.post(`/projects/${pid}/inspect-host`, {
        host: value.host.trim(), port: Number(value.port) || 22, username: value.username.trim(),
        pem_key: pem ? value.pem_key : null, password: pem ? (value.password || null) : value.password,
        remote_workdir: value.remote_workdir.trim() || "~", venv_path: value.env_mode === "existing" ? value.venv_path : null,
      });
      setInfo(data);
      toast.success("Connected to the host");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not connect to the host");
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="mt-4 p-4 border border-[#27272A] bg-[#050505] space-y-4" data-testid="remote-train-fields">
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="md:col-span-2">
          <Label className={LABEL}>Host</Label>
          <Input value={value.host} onChange={set("host")} placeholder="203.0.113.10" className={FIELD} data-testid={REMOTE_TRAIN.host} />
        </div>
        <div>
          <Label className={LABEL}>Port</Label>
          <Input type="number" value={value.port} onChange={set("port")} className={FIELD} data-testid={REMOTE_TRAIN.port} />
        </div>
        <div>
          <Label className={LABEL}>Username</Label>
          <Input value={value.username} onChange={set("username")} className={FIELD} data-testid={REMOTE_TRAIN.username} />
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div>
          <Label className={LABEL}>Sign in with</Label>
          <Select value={value.authMode} onValueChange={(v) => onChange({ ...value, authMode: v })}>
            <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]"><SelectValue /></SelectTrigger>
            <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
              <SelectItem value="password">Password</SelectItem>
              <SelectItem value="pem">Private key (PEM)</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="md:col-span-3">
          <Label className={LABEL}>{value.authMode === "pem" ? "Key passphrase (if any)" : "Password"}</Label>
          <Input type="password" autoComplete="off" value={value.password} onChange={set("password")} className={FIELD} data-testid={REMOTE_TRAIN.password} />
        </div>
      </div>
      {value.authMode === "pem" && (
        <div>
          <Label className={LABEL}>Private key</Label>
          <textarea rows={4} value={value.pem_key} onChange={set("pem_key")} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
            className="w-full rounded-sm bg-transparent border border-[#27272A] text-xs p-2 font-mono focus:outline-none focus:border-primary" data-testid={REMOTE_TRAIN.pemKey} />
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="md:col-span-2">
          <Label className={LABEL}>Working folder on the host</Label>
          <Input value={value.remote_workdir} onChange={set("remote_workdir")} placeholder="~/thinkvision" className={FIELD} data-testid={REMOTE_TRAIN.workdir} />
        </div>
        <div>
          <Label className={LABEL}>Python environment</Label>
          <Select value={value.env_mode} onValueChange={(v) => onChange({ ...value, env_mode: v })}>
            <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid={REMOTE_TRAIN.envMode}><SelectValue /></SelectTrigger>
            <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
              <SelectItem value="system">System python3</SelectItem>
              <SelectItem value="existing">Existing venv</SelectItem>
              <SelectItem value="create">Create venv</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {value.env_mode === "existing" && (
          <div>
            <Label className={LABEL}>Venv folder</Label>
            <Input value={value.venv_path} onChange={set("venv_path")} placeholder="~/venv" className={FIELD} data-testid={REMOTE_TRAIN.venvPath} />
          </div>
        )}
      </div>

      <div className="flex items-center gap-3">
        <Button type="button" size="sm" variant="outline" onClick={checkHost} disabled={checking || !value.host || !value.username}
          className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] border-[#27272A] bg-transparent" data-testid={REMOTE_TRAIN.checkHost}>
          {checking ? <Loader2 className="w-3 h-3 mr-2 animate-spin" /> : null} Check host
        </Button>
        <span className="text-[10px] text-muted-foreground">Credentials are used for this run only and are never stored.</span>
      </div>
      {info && (
        <div className="text-[10px] text-muted-foreground" data-testid={REMOTE_TRAIN.hostInfo}>
          Connected · {info.venv_ok === false ? "venv not found" : info.venv_ok ? "venv OK" : "no venv checked"} · {(info.models || []).length} model file(s) found
        </div>
      )}
    </div>
  );
}
