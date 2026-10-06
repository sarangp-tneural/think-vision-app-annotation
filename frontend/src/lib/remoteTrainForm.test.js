import { validateRemoteForm, remoteTrainBody } from "./remoteTrainForm";

const ok = { host: "10.0.0.5", port: 22, username: "ubuntu", authMode: "password", password: "pw", remote_workdir: "~/tv", env_mode: "system" };

test("a complete form is valid", () => expect(validateRemoteForm(ok)).toBeNull());

test("reports the first missing piece", () => {
  expect(validateRemoteForm({ ...ok, host: " " })).toMatch(/host/i);
  expect(validateRemoteForm({ ...ok, username: "" })).toMatch(/username/i);
  expect(validateRemoteForm({ ...ok, password: "" })).toMatch(/password/i);
  expect(validateRemoteForm({ ...ok, authMode: "pem", pem_key: "" })).toMatch(/key/i);
  expect(validateRemoteForm({ ...ok, remote_workdir: "" })).toMatch(/folder/i);
  expect(validateRemoteForm({ ...ok, env_mode: "existing", venv_path: "" })).toMatch(/environment/i);
  expect(validateRemoteForm({ ...ok, port: 70000 })).toMatch(/port/i);
});

test("the body carries only the chosen credential and the venv only for an existing env", () => {
  const b = remoteTrainBody({ ...ok, pem_key: "KEY" }, "yolov8s", 5);
  expect(b).toMatchObject({ pem_key: null, password: "pw", venv_path: null, model_arch: "yolov8s", epochs: 5 });
  const k = remoteTrainBody({ ...ok, authMode: "pem", pem_key: "KEY", password: "", env_mode: "existing", venv_path: " /v " }, "yolov8n", 0);
  expect(k).toMatchObject({ pem_key: "KEY", password: null, venv_path: "/v" });
});
