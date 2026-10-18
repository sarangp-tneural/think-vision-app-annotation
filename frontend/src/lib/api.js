import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

export const api = axios.create({ baseURL: API });

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("vf_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem("vf_token");
      localStorage.removeItem("vf_user");
      if (window.location.pathname !== "/" && !window.location.pathname.startsWith("/auth")) {
        window.location.href = "/auth";
      }
    }
    return Promise.reject(err);
  }
);

export const getToken = () => localStorage.getItem("vf_token");
export const getUser = () => {
  const u = localStorage.getItem("vf_user");
  return u ? JSON.parse(u) : null;
};
export const USER_CHANGED_EVENT = "vf-user-changed";
const notifyUserChanged = () => window.dispatchEvent(new Event(USER_CHANGED_EVENT));
export const setAuth = (token, user) => {
  localStorage.setItem("vf_token", token);
  localStorage.setItem("vf_user", JSON.stringify(user));
  notifyUserChanged();
};
// Merge new fields into the stored user (e.g. after a profile edit) and tell mounted components.
export const updateUser = (patch) => {
  localStorage.setItem("vf_user", JSON.stringify({ ...getUser(), ...patch }));
  notifyUserChanged();
};
export const clearAuth = () => {
  localStorage.removeItem("vf_token");
  localStorage.removeItem("vf_user");
};

// Build authenticated file URL (uses ?auth=token because <img> can't send headers)
export const fileUrl = (storagePath) => {
  const token = getToken();
  return `${API}/files/${storagePath}?auth=${encodeURIComponent(token || "")}`;
};

// Avatar image URL for a user object ({id, avatar_path, avatar_v}); null when they have none.
export const avatarUrl = (user) => {
  if (!user?.avatar_path) return null;
  return `${API}/users/${user.id}/avatar?auth=${encodeURIComponent(getToken() || "")}&v=${user.avatar_v || 0}`;
};
