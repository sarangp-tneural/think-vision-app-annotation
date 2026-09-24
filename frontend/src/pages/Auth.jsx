import { useState, useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { api, setAuth, getToken } from "@/lib/api";
import { ArrowRight, Terminal } from "lucide-react";

export default function Auth() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [mode, setMode] = useState(params.get("mode") === "register" ? "register" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [allowedDomain, setAllowedDomain] = useState(null);

  useEffect(() => {
    if (getToken()) navigate("/dashboard");
    // Fetch domain restriction hint (public endpoint)
    api.get("/system/versions").then(({ data }) => {
      if (data?.allowed_email_domain) setAllowedDomain(data.allowed_email_domain);
    }).catch(() => {});
  }, [navigate]);

  const submit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const url = mode === "login" ? "/auth/login" : "/auth/register";
      const payload = mode === "login" ? { email, password } : { email, password, name };
      const { data } = await api.post(url, payload);
      setAuth(data.token, data.user);
      toast.success(mode === "login" ? "Welcome back" : "Account created");
      navigate("/dashboard");
    } catch (err) {
      toast.error(err.response?.data?.detail || "Authentication failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background flex">
      {/* Left: brand panel */}
      <div className="hidden lg:flex lg:w-1/2 border-r border-[#27272A] relative overflow-hidden">
        <div className="absolute inset-0 grid-bg opacity-40" />
        <div className="relative z-10 p-16 flex flex-col justify-between w-full">
          <div className="flex items-center gap-2" data-testid="auth-logo">
            <div className="w-6 h-6 bg-primary flex items-center justify-center">
              <div className="w-2 h-2 bg-black"></div>
            </div>
            <span className="font-heading font-bold text-lg tracking-tight">THINKVISION</span>
          </div>

          <div className="max-w-lg">
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-6">// Build vision</div>
            <h2 className="font-heading text-4xl xl:text-5xl font-bold tracking-tight leading-[1.05] mb-6">
              Datasets are the<br /> new source code.
            </h2>
            <p className="text-sm text-muted-foreground leading-relaxed mb-10">
              Curate, annotate, version and deploy computer vision models in a single workflow.
              Powered by Gemini vision for instant labeling.
            </p>

            <div className="panel p-4 font-mono text-[11px] leading-relaxed">
              <div className="flex items-center gap-2 mb-3 text-muted-foreground">
                <Terminal className="w-3 h-3" />
                <span>~/thinkvision</span>
              </div>
              <div className="text-primary">$ vf projects create traffic-detection</div>
              <div className="text-muted-foreground">→ project created · id=proj_a3f7</div>
              <div className="text-primary">$ vf auto-label --dataset ./images</div>
              <div className="text-muted-foreground">→ 1,247 boxes generated in 12s<span className="cursor-blink">_</span></div>
            </div>
          </div>

          <div className="text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
            © 2026 ThinkVision · Made for ML engineers
          </div>
        </div>
      </div>

      {/* Right: form */}
      <div className="flex-1 flex items-center justify-center p-6 md:p-16">
        <div className="w-full max-w-md fade-in-up">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">
            // {mode === "login" ? "Access console" : "Create account"}
          </div>
          <h1 className="font-heading text-3xl md:text-4xl font-bold mb-2 tracking-tight">
            {mode === "login" ? "Welcome back." : "Get started."}
          </h1>
          <p className="text-sm text-muted-foreground mb-10">
            {mode === "login" ? "Continue where you left off." : "No credit card required."}
          </p>

          <form onSubmit={submit} className="space-y-5">
            {mode === "register" && (
              <div>
                <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Name</Label>
                <Input
                  type="text"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="rounded-sm bg-transparent border-[#27272A] h-11 focus-visible:ring-primary focus-visible:ring-2"
                  placeholder="Jane Doe"
                  data-testid="auth-name-input"
                />
              </div>
            )}
            <div>
              <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Email</Label>
              <Input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="rounded-sm bg-transparent border-[#27272A] h-11 focus-visible:ring-primary focus-visible:ring-2"
                placeholder={allowedDomain ? `you@${allowedDomain}` : "you@company.com"}
                data-testid="auth-email-input"
              />
              {allowedDomain && (
                <div className="text-[10px] text-muted-foreground mt-1.5" data-testid="auth-domain-hint">
                  Restricted to <span className="text-primary">@{allowedDomain}</span> addresses
                </div>
              )}
            </div>
            <div>
              <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Password</Label>
              <Input
                type="password"
                required
                minLength={6}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="rounded-sm bg-transparent border-[#27272A] h-11 focus-visible:ring-primary focus-visible:ring-2"
                placeholder="min 6 characters"
                data-testid="auth-password-input"
              />
            </div>

            <Button
              type="submit"
              disabled={loading}
              className="w-full rounded-sm bg-primary text-black hover:bg-cyan-400 h-11 text-xs uppercase tracking-[0.2em] font-bold"
              data-testid="auth-submit-btn"
            >
              {loading ? "Processing..." : (mode === "login" ? "Sign In" : "Create Account")}
              {!loading && <ArrowRight className="w-4 h-4 ml-2" />}
            </Button>
          </form>

          <div className="mt-8 pt-8 border-t border-[#27272A] text-center">
            <button
              onClick={() => setMode(mode === "login" ? "register" : "login")}
              className="text-xs text-muted-foreground hover:text-primary transition-colors"
              data-testid="auth-toggle-mode-btn"
            >
              {mode === "login" ? "Don't have an account? Sign up →" : "Already have an account? Sign in →"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
