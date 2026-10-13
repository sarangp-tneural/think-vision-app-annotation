import { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { api } from "@/lib/api";
import Navbar from "@/components/Navbar";
import DeployPipeline from "@/pages/DeployPipeline";
import { toast } from "sonner";

// Owners/admins only, matching who can run the pipeline.
export default function DeployPipelinePage() {
  const { pid } = useParams();
  const [project, setProject] = useState(null);
  const [myRole, setMyRole] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const { data: p } = await api.get(`/projects/${pid}`);
        setProject(p);
        if (p.team_id) {
          const { data: teams } = await api.get("/teams");
          setMyRole(teams.find((t) => t.id === p.team_id)?.role || "none");
        } else { setMyRole("owner"); }
      } catch { toast.error("Failed to load"); }
    })();
  }, [pid]);

  if (!project || !myRole) return <div className="min-h-screen bg-background"><Navbar /></div>;
  if (myRole !== "owner" && myRole !== "admin") return <Navigate replace to={`/projects/${pid}/deploy`} />;

  return (
    <div className="min-h-screen bg-background">
      <Navbar crumbs={[
        { label: "Projects", to: "/dashboard" },
        { label: project.name, to: `/projects/${pid}` },
        { label: "Deploy Pipeline" },
      ]} />
      <main className="max-w-[1600px] mx-auto px-6 py-12">
        <div className="mb-10">
          <div className="text-[10px] uppercase tracking-[0.3em] text-primary mb-3">// Model deployment</div>
          <h1 className="font-heading text-4xl font-bold tracking-tight mb-2">Deploy Pipeline</h1>
          <p className="text-sm text-muted-foreground">
            Train on a remote server, test against the active model, then approve and deploy.
          </p>
        </div>
        <DeployPipeline pid={pid} project={project} />
      </main>
    </div>
  );
}
