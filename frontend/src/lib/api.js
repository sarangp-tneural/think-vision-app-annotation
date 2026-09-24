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
export const setAuth = (token, user) => {
  localStorage.setItem("vf_token", token);
  localStorage.setItem("vf_user", JSON.stringify(user));
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
