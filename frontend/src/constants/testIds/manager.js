export const MANAGER = {
  loginTab: "auth-manager-tab",
  emailInput: "auth-manager-email-input",
  nav: (k) => `manager-nav-${k}`,
  logout: "manager-logout-btn",
  rangePreset: (d) => `manager-range-${d}`,
  rangeFrom: "manager-range-from",
  rangeTo: "manager-range-to",
  exportCsv: "manager-export-btn",
  kpi: (k) => `manager-kpi-${k}`,
  personRow: (id) => `manager-person-row-${id}`,
  projectRow: (key) => `manager-project-row-${key}`,
  search: "manager-search-input",
};

export const WORK_LOG = {
  addBtn: "worklog-add-btn",
  importInput: "worklog-import-input",
  date: "worklog-date-input",
  project: "worklog-project-input",
  task: "worklog-task-input",
  frames: "worklog-frames-input",
  verifiedBy: "worklog-verified-input",
  save: "worklog-save-btn",
  row: (id) => `worklog-row-${id}`,
  delete: (id) => `worklog-delete-${id}`,
};
