import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Zap, Scan, Layers, GitBranch, Rocket, Terminal, Sparkles, ArrowRight } from "lucide-react";

export default function Landing() {
  const navigate = useNavigate();

  const features = [
    { icon: Scan, title: "Annotate", desc: "Draw bounding boxes with precision. Multi-class labeling with sub-pixel accuracy." },
    { icon: Sparkles, title: "Auto-Label", desc: "Gemini-powered vision detects objects instantly. Ship datasets 10x faster." },
    { icon: Layers, title: "Version", desc: "Snapshot dataset states. Reproducible pipelines with immutable versioning." },
    { icon: GitBranch, title: "Export", desc: "COCO, YOLO, Pascal VOC. One-click zip download for any training framework." },
    { icon: Rocket, title: "Deploy", desc: "Simulate training runs. Monitor mAP, loss, precision & recall curves live." },
    { icon: Terminal, title: "API", desc: "REST endpoints for every project. Programmatic inference at your fingertips." },
  ];

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Nav */}
      <header className="border-b border-[#27272A]">
        <div className="max-w-[1600px] mx-auto flex items-center justify-between px-6 py-5">
          <div className="flex items-center gap-2" data-testid="landing-logo">
            <div className="w-6 h-6 bg-primary flex items-center justify-center">
              <div className="w-2 h-2 bg-black"></div>
            </div>
            <span className="font-heading font-bold text-lg tracking-tight">THINKVISION</span>
          </div>
          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              onClick={() => navigate("/auth")}
              className="rounded-sm text-xs uppercase tracking-[0.2em] hover:text-primary"
              data-testid="landing-login-btn"
            >
              Log In
            </Button>
            <Button
              onClick={() => navigate("/auth?mode=register")}
              className="rounded-sm bg-primary text-black hover:bg-cyan-400 text-xs uppercase tracking-[0.2em] font-bold"
              data-testid="landing-cta-btn"
            >
              Start Building
            </Button>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative overflow-hidden">
        <div className="absolute inset-0 grid-bg grid-bg-fade pointer-events-none" />
        <div className="relative max-w-[1600px] mx-auto px-6 py-24 md:py-32">
          <div className="max-w-4xl fade-in-up">
            <div className="inline-flex items-center gap-2 border border-[#27272A] bg-[#121212] px-3 py-1 mb-8">
              <Zap className="w-3 h-3 text-primary" />
              <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                v1.0 · Gemini Vision Enabled
              </span>
            </div>

            <h1 className="font-heading text-4xl sm:text-5xl lg:text-6xl tracking-tight font-bold leading-[1.05] mb-6">
              The computer vision<br />
              <span className="text-primary">operating system</span> for<br />
              production teams<span className="text-primary cursor-blink">_</span>
            </h1>

            <p className="text-sm md:text-base leading-relaxed text-muted-foreground max-w-2xl mb-10">
              Annotate images, generate datasets, and deploy vision models — all in one workflow.
              Auto-label with Gemini, version your data, export to YOLO/COCO/VOC, then ship.
            </p>

            <div className="flex flex-wrap items-center gap-4">
              <Button
                onClick={() => navigate("/auth?mode=register")}
                className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-11 px-6 text-xs uppercase tracking-[0.2em] font-bold"
                data-testid="hero-start-btn"
              >
                Start Free <ArrowRight className="w-4 h-4 ml-2" />
              </Button>
              <Button
                variant="outline"
                onClick={() => navigate("/auth")}
                className="rounded-sm h-11 px-6 border-[#27272A] bg-transparent text-xs uppercase tracking-[0.2em] hover:bg-[#1C1C1C] hover:border-primary hover:text-primary"
                data-testid="hero-login-btn"
              >
                Sign In
              </Button>
            </div>

            <div className="mt-16 flex flex-wrap items-center gap-8 text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
              <span>·  Object Detection</span>
              <span>·  Instance Segmentation</span>
              <span>·  Classification</span>
              <span>·  Auto-Labeling</span>
            </div>
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="border-t border-[#27272A]">
        <div className="max-w-[1600px] mx-auto px-6 py-20">
          <div className="max-w-2xl mb-16">
            <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-4">// The stack</div>
            <h2 className="font-heading text-3xl md:text-4xl font-semibold tracking-tight leading-tight">
              Every tool your CV team needs.<br />Wired together.
            </h2>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-0 border-l border-t border-[#27272A]">
            {features.map((f) => (
              <div
                key={f.title}
                className="p-8 border-r border-b border-[#27272A] panel-hover"
                data-testid={`feature-card-${f.title.toLowerCase()}`}
              >
                <f.icon className="w-6 h-6 text-primary mb-6" strokeWidth={1.5} />
                <h3 className="font-heading text-xl font-medium mb-3">{f.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-[#27272A]">
        <div className="max-w-[1600px] mx-auto px-6 py-24 text-center">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-6">// Get started</div>
          <h2 className="font-heading text-3xl md:text-5xl font-bold tracking-tight mb-6">
            Your first dataset,<br /> in five minutes.
          </h2>
          <p className="text-sm text-muted-foreground max-w-xl mx-auto mb-10">
            Free forever. No credit card. Open pipelines from day one.
          </p>
          <Button
            onClick={() => navigate("/auth?mode=register")}
            className="rounded-sm bg-primary text-black hover:bg-cyan-400 h-12 px-8 text-xs uppercase tracking-[0.2em] font-bold"
            data-testid="cta-start-btn"
          >
            Create Free Account <ArrowRight className="w-4 h-4 ml-2" />
          </Button>
        </div>
      </section>

      <footer className="border-t border-[#27272A]">
        <div className="max-w-[1600px] mx-auto px-6 py-8 flex items-center justify-between text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
          <span>© 2026 ThinkVision</span>
          <span>Built for CV engineers</span>
        </div>
      </footer>
    </div>
  );
}
