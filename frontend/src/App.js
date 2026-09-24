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
import { getToken } from "@/lib/api";

const Protected = ({ children }) => {
  if (!getToken()) return <Navigate to="/auth" replace />;
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
