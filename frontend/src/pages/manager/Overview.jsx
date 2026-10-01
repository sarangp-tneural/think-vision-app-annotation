import { useNavigate, useOutletContext } from "react-router-dom";
import { Kpi, Section, DailyChart, RankBars, StatusBar, useManagerData, fmt, pct, Loading } from "./shared";

export default function Overview() {
  const range = useOutletContext();
  const navigate = useNavigate();
  const { data, loading } = useManagerData("/manager/overview", range);
  if (loading || !data) return <Loading />;
  const k = data.kpis;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Kpi id="frames" label="Frames annotated" value={fmt(k.frames)} hint={`${fmt(k.tracked_frames)} tracked · ${fmt(k.logged_frames)} logged`} />
        <Kpi id="people" label="Active people" value={fmt(k.active_people)} hint={`of ${fmt(k.total_people)} total`} />
        <Kpi id="projects" label="Active projects" value={fmt(k.active_projects)} hint={`of ${fmt(k.total_projects)} total`} />
        <Kpi id="approval" label="Approval rate" value={pct(k.approval_rate)} hint={`${fmt(k.approved)} approved · ${fmt(k.rejected)} rejected`} />
      </div>
      <Section title="Frames per day"><DailyChart data={data.daily} /></Section>
      <div className="grid lg:grid-cols-2 gap-6">
        <Section title="Top people"><RankBars rows={data.top_people} labelKey="name" onClick={(r) => navigate(`/manager/people/${r.user_id}`)} /></Section>
        <Section title="Top projects"><RankBars rows={data.top_projects} labelKey="project" /></Section>
      </div>
      <Section title="Review pipeline (all images)"><StatusBar review={data.review_pipeline} /></Section>
    </div>
  );
}
