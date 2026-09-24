import { Link, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { LogOut, LayoutGrid, User as UserIcon, Users } from "lucide-react";
import { clearAuth, getUser } from "@/lib/api";
import NotificationsBell from "@/components/NotificationsBell";

export default function Navbar({ crumbs }) {
  const navigate = useNavigate();
  const user = getUser();

  const logout = () => {
    clearAuth();
    navigate("/");
  };

  return (
    <header className="sticky top-0 z-40 backdrop-blur-xl bg-[#050505]/80 border-b border-[#27272A]">
      <div className="max-w-[1600px] mx-auto flex items-center justify-between px-6 py-4">
        <div className="flex items-center gap-6">
          <Link to="/dashboard" className="flex items-center gap-2" data-testid="navbar-logo">
            <div className="w-6 h-6 bg-primary rounded-none flex items-center justify-center">
              <div className="w-2 h-2 bg-black"></div>
            </div>
            <span className="font-heading font-bold text-lg tracking-tight">THINKVISION</span>
          </Link>
          {crumbs && crumbs.length > 0 && (
            <div className="hidden md:flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-muted-foreground">
              {crumbs.map((c, i) => (
                <span key={c.label + (c.to || "")} className="flex items-center gap-2">
                  {i > 0 && <span className="text-[#3f3f46]">/</span>}
                  {c.to ? (
                    <Link to={c.to} className="hover:text-primary transition-colors">{c.label}</Link>
                  ) : (
                    <span className="text-foreground">{c.label}</span>
                  )}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          <Link to="/dashboard" className="text-xs uppercase tracking-[0.2em] text-muted-foreground hover:text-primary transition-colors flex items-center gap-2" data-testid="navbar-projects-link">
            <LayoutGrid className="w-4 h-4" /> Projects
          </Link>
          <Link to="/teams" className="text-xs uppercase tracking-[0.2em] text-muted-foreground hover:text-primary transition-colors flex items-center gap-2" data-testid="navbar-teams-link">
            <Users className="w-4 h-4" /> Teams
          </Link>
          <NotificationsBell />
          {user && (
            <div className="hidden md:flex items-center gap-2 text-xs text-muted-foreground border-l border-[#27272A] pl-3" data-testid="navbar-user">
              <UserIcon className="w-4 h-4" />
              <span>{user.name}</span>
            </div>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={logout}
            className="rounded-sm h-8 text-xs uppercase tracking-[0.2em] hover:bg-[#1C1C1C] hover:text-primary"
            data-testid="navbar-logout-btn"
          >
            <LogOut className="w-4 h-4 mr-2" /> Exit
          </Button>
        </div>
      </div>
    </header>
  );
}
