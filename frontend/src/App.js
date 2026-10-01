import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import Landing from "@/pages/Landing";
import Auth from "@/pages/Auth";
import Dashboard from "@/pages/Dashboard";
import ProjectDetail from "@/pages/ProjectDetail";
import Annotator from "@/pages/Annotator";
import Versions from "@/pages/Versions";
import ModelDeploy from "@/pages/ModelDeploy";
import Teams from "@/pages/Teams";
import TeamDetail from "@/pages/TeamDetail";
import TeamDashboard from "@/pages/TeamDashboard";
import Activity from "@/pages/Activity";
import MyWorkLog from "@/pages/MyWorkLog";
import ManagerLayout from "@/pages/manager/ManagerLayout";
import ManagerOverview from "@/pages/manager/Overview";
import ManagerPeople from "@/pages/manager/People";
import ManagerPersonDetail from "@/pages/manager/PersonDetail";
import ManagerProjects from "@/pages/manager/Projects";
import ManagerProjectDetail from "@/pages/manager/ProjectDetail";
import ManagerWorkLog from "@/pages/manager/WorkLog";
import { getToken, getUser } from "@/lib/api";

const Protected = ({ children }) => {
  if (!getToken()) return <Navigate to="/auth" replace />;
  return children;
};

// Server enforces the role on every /manager API call; this only keeps the UI tidy.
const ManagerProtected = ({ children }) => {
  if (!getToken()) return <Navigate to="/auth" replace />;
  if (getUser()?.role !== "manager") return <Navigate to="/dashboard" replace />;
  return children;
};

function App() {
  return (
    <div className="App">
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/auth" element={<Auth />} />
          <Route path="/dashboard" element={<Protected><Dashboard /></Protected>} />
          <Route path="/work-log" element={<Protected><MyWorkLog /></Protected>} />
          <Route path="/manager" element={<ManagerProtected><ManagerLayout /></ManagerProtected>}>
            <Route index element={<ManagerOverview />} />
            <Route path="people" element={<ManagerPeople />} />
            <Route path="people/:uid" element={<ManagerPersonDetail />} />
            <Route path="projects" element={<ManagerProjects />} />
            <Route path="projects/:pid" element={<ManagerProjectDetail />} />
            <Route path="work-log" element={<ManagerWorkLog />} />
          </Route>
          <Route path="/teams" element={<Protected><Teams /></Protected>} />
          <Route path="/teams/:tid" element={<Protected><TeamDetail /></Protected>} />
          <Route path="/teams/:tid/dashboard" element={<Protected><TeamDashboard /></Protected>} />
          <Route path="/projects/:pid" element={<Protected><ProjectDetail /></Protected>} />
          <Route path="/projects/:pid/activity" element={<Protected><Activity /></Protected>} />
          <Route path="/projects/:pid/annotate/:imgId" element={<Protected><Annotator /></Protected>} />
          <Route path="/projects/:pid/versions" element={<Protected><Versions /></Protected>} />
          <Route path="/projects/:pid/deploy" element={<Protected><ModelDeploy /></Protected>} />
        </Routes>
      </BrowserRouter>
      <Toaster theme="dark" position="top-right" />
    </div>
  );
}

export default App;
