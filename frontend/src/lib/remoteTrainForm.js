// Client-side checks for the "train on a remote host" form; the server validates again.
export function validateRemoteForm(f) {
  if (!f.host?.trim()) return "Enter the host address";
  if (!f.username?.trim()) return "Enter the SSH username";
  if (f.authMode === "pem" ? !f.pem_key?.trim() : !f.password) return f.authMode === "pem" ? "Paste the private key" : "Enter the SSH password";
  if (!f.remote_workdir?.trim()) return "Enter a working folder on the host";
  if (f.env_mode === "existing" && !f.venv_path?.trim()) return "Enter the path of the existing virtual environment";
  const port = Number(f.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "Port must be between 1 and 65535";
  return null;
}

// Body for POST /projects/{pid}/train-remote. Only the credential that matches the chosen auth mode is sent.
export function remoteTrainBody(f, modelArch, epochs) {
  const pem = f.authMode === "pem";
  return {
    host: f.host.trim(), port: Number(f.port) || 22, username: f.username.trim(),
    pem_key: pem ? f.pem_key : null,
    password: pem ? (f.password || null) : f.password, // with a key, "password" is its passphrase
    remote_workdir: f.remote_workdir.trim(), env_mode: f.env_mode,
    venv_path: f.env_mode === "existing" ? f.venv_path.trim() : null,
    model_arch: modelArch, epochs,
  };
}
