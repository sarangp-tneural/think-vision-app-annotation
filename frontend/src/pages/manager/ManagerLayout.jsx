import { useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { LayoutDashboard, Users, FolderKanban, ClipboardList, LogOut, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, clearAuth, getUser } from "@/lib/api";
import { toast } from "sonner";
import { MANAGER, PROFILE } from "@/constants/testIds";

const NAV = [
  { key: "overview", to: "/manager", label: "Overview", icon: LayoutDashboard, end: true },
  { key: "people", to: "/manager/people", label: "People", icon: Users },
  { key: "projects", to: "/manager/projects", label: "Projects", icon: FolderKanban },
  { key: "work-log", to: "/manager/work-log", label: "Work log", icon: ClipboardList },
];

const iso = (d) => d.toISOString().slice(0, 10);
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };

export default function ManagerLayout() {
  const navigate = useNavigate();
  const user = getUser();
  const [range, setRange] = useState({ from: daysAgo(29), to: iso(new Date()), preset: 30 });

  const preset = (n) => setRange({ from: daysAgo(n - 1), to: iso(new Date()), preset: n });
  const custom = (k, v) => v && setRange((r) => ({ ...r, [k]: v, preset: null }));

  const logout = () => { clearAuth(); navigate("/auth"); };

  const exportCsv = async () => {
    try {
      const { data } = await api.get("/manager/export.csv", { params: { from: range.from, to: range.to }, responseType: "blob" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(data);
      a.download = `thinkvision_${range.from}_${range.to}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch { toast.error("Export failed"); }
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-40 backdrop-blur-xl bg-[#050505]/80 border-b border-[#27272A]">
        <div className="max-w-[1400px] mx-auto px-6 py-4 flex flex-wrap items-center gap-x-8 gap-y-3">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 bg-primary flex items-center justify-center"><div className="w-2 h-2 bg-black" /></div>
            <span className="font-heading font-bold text-lg tracking-tight">THINKVISION</span>
            <span className="text-[10px] uppercase tracking-[0.3em] text-primary ml-2">Manager</span>
          </div>
          <nav className="flex items-center gap-1 flex-1">
            {NAV.map(({ key, to, label, icon: Icon, end }) => (
              <NavLink key={key} to={to} end={end} data-testid={MANAGER.nav(key)}
                className={({ isActive }) => `flex items-center gap-2 px-3 py-2 text-xs uppercase tracking-[0.2em] transition-colors ${isActive ? "text-primary bg-[#121212]" : "text-muted-foreground hover:text-foreground"}`}>
                <Icon className="w-4 h-4" />{label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <Link to="/profile" className="hidden md:inline hover:text-primary transition-colors" data-testid={PROFILE.navbarLink}>{user?.name}</Link>
            <Button variant="ghost" size="sm" onClick={logout} className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] hover:bg-[#1C1C1C] hover:text-primary" data-testid={MANAGER.logout}>
              <LogOut className="w-4 h-4 mr-2" /> Exit
            </Button>
          </div>
        </div>
        <div className="max-w-[1400px] mx-auto px-6 pb-3 flex flex-wrap items-center gap-3">
          {[7, 30, 90].map((n) => (
            <button key={n} onClick={() => preset(n)} data-testid={MANAGER.rangePreset(n)}
              className={`px-3 h-8 text-xs border ${range.preset === n ? "border-primary text-primary" : "border-[#27272A] text-muted-foreground hover:text-foreground"}`}>
              Last {n}d
            </button>
          ))}
          <Input type="date" value={range.from} max={range.to} onChange={(e) => custom("from", e.target.value)} className="w-40 h-8 rounded-sm bg-transparent border-[#27272A] text-xs" data-testid={MANAGER.rangeFrom} aria-label="From date" />
          <span className="text-muted-foreground text-xs">→</span>
          <Input type="date" value={range.to} min={range.from} onChange={(e) => custom("to", e.target.value)} className="w-40 h-8 rounded-sm bg-transparent border-[#27272A] text-xs" data-testid={MANAGER.rangeTo} aria-label="To date" />
          <Button variant="outline" size="sm" onClick={exportCsv} className="ml-auto rounded-sm h-8 text-xs border-[#27272A]" data-testid={MANAGER.exportCsv}>
            <Download className="w-4 h-4 mr-2" /> Export CSV
          </Button>
        </div>
      </header>
      <main className="max-w-[1400px] mx-auto px-6 py-8">
        <Outlet context={{ from: range.from, to: range.to }} />
      </main>
    </div>
  );
}
