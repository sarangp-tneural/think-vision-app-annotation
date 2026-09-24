"""Backend smoke test for VisionForge full end-to-end flow."""
import io
import json
import os
import time
import zipfile
import requests
from PIL import Image

BASE = "https://ai-vision-hub-20.preview.emergentagent.com/api"
EMAIL = "demo@vf.io"
PASSWORD = "demo1234"
NAME = "Demo User"

results = []
def log(step, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {step} :: {detail}")
    results.append({"step": step, "ok": ok, "detail": detail})

session = requests.Session()

# 1. Register or login
def extract_token(js):
    return js.get("access_token") or js.get("token")

r = requests.post(f"{BASE}/auth/register", json={"email": EMAIL, "password": PASSWORD, "name": NAME}, timeout=15)
if r.status_code == 200:
    token = extract_token(r.json())
    user = r.json().get("user")
    log("register", bool(token) and bool(user), f"status={r.status_code} has_token={bool(token)} has_user={bool(user)}")
else:
    r2 = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    if r2.status_code == 200:
        token = extract_token(r2.json())
        log("register(fallback login - user already exists)", bool(token), f"register={r.status_code}, login=200, has_token={bool(token)}")
    else:
        log("register/login", False, f"register={r.status_code} body={r.text[:200]} login={r2.status_code} body={r2.text[:200]}")
        token = None

assert token, "No token obtained"
headers = {"Authorization": f"Bearer {token}"}

# 2. Login explicit
r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
token_ok = r.status_code == 200 and extract_token(r.json())
log("login", bool(token_ok), f"status={r.status_code}")
token = extract_token(r.json())
headers = {"Authorization": f"Bearer {token}"}

# 3. /auth/me
r = requests.get(f"{BASE}/auth/me", headers=headers, timeout=15)
log("auth/me", r.status_code == 200 and r.json().get("email") == EMAIL, f"status={r.status_code} body={r.text[:150]}")

# 4. Create project
r = requests.post(f"{BASE}/projects", headers=headers,
                  json={"name": "Test Project", "description": "Smoke test", "task_type": "object_detection"},
                  timeout=15)
log("create project", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
project = r.json() if r.status_code == 200 else {}
pid = project.get("id")
assert pid, f"No project id, body={r.text[:200]}"

# 5. List projects
r = requests.get(f"{BASE}/projects", headers=headers, timeout=15)
ok = r.status_code == 200 and isinstance(r.json(), list) and any(p.get("id") == pid for p in r.json())
sample = r.json()[0] if r.status_code == 200 and r.json() else {}
log("list projects", ok, f"status={r.status_code} count={len(r.json()) if r.status_code==200 else '?'} keys={list(sample.keys())[:8]}")

# 6. Get single project
r = requests.get(f"{BASE}/projects/{pid}", headers=headers, timeout=15)
log("get project", r.status_code == 200 and r.json().get("id") == pid, f"status={r.status_code}")

# 7. Add class
r = requests.post(f"{BASE}/projects/{pid}/classes", headers=headers, json={"label": "cat"}, timeout=15)
log("add class", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")

# 8. Upload image
img = Image.new("RGB", (400, 300), (200, 60, 60))
buf = io.BytesIO()
img.save(buf, format="JPEG")
buf.seek(0)
files = {"file": ("test.jpg", buf, "image/jpeg")}
r = requests.post(f"{BASE}/projects/{pid}/images", headers=headers, files=files, timeout=60)
log("upload image", r.status_code == 200, f"status={r.status_code} body={r.text[:250]}")
img_obj = r.json() if r.status_code == 200 else {}
img_id = img_obj.get("id")
img_path = img_obj.get("storage_path") or img_obj.get("path")

# 9. List images
r = requests.get(f"{BASE}/projects/{pid}/images", headers=headers, timeout=15)
log("list images", r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) >= 1, f"status={r.status_code} count={len(r.json()) if r.status_code==200 else '?'}")

# 10. Get file bytes
if img_path:
    r = requests.get(f"{BASE}/files/{img_path}", headers=headers, timeout=30)
    log("get file (header auth)", r.status_code == 200 and len(r.content) > 100, f"status={r.status_code} bytes={len(r.content)}")
    r = requests.get(f"{BASE}/files/{img_path}?auth={token}", timeout=30)
    log("get file (query auth)", r.status_code == 200 and len(r.content) > 100, f"status={r.status_code} bytes={len(r.content)}")
else:
    log("get file", False, "no img path")

# 11. Save annotations
if img_id:
    ann = [{"label": "cat", "x": 0.05, "y": 0.05, "w": 0.3, "h": 0.3}]
    r = requests.put(f"{BASE}/images/{img_id}/annotations", headers=headers, json={"boxes": ann}, timeout=15)
    log("save annotations", r.status_code == 200, f"status={r.status_code} body={r.text[:250]}")

# 12. Auto-label (Gemini)
if img_id:
    print("... calling auto-label (may take 10-30s)")
    t0 = time.time()
    try:
        r = requests.post(f"{BASE}/images/{img_id}/auto-label", headers=headers, timeout=90)
        elapsed = time.time() - t0
        ok = r.status_code == 200
        body = r.json() if ok else {}
        log("auto-label (Gemini)", ok, f"status={r.status_code} elapsed={elapsed:.1f}s body_keys={list(body.keys()) if isinstance(body,dict) else 'list'} body={str(body)[:200]}")
    except Exception as e:
        log("auto-label (Gemini)", False, f"exception={e}")

# 13. Create version
r = requests.post(f"{BASE}/projects/{pid}/versions", headers=headers, json={"name": "v1", "description": "initial"}, timeout=15)
log("create version", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
version_id = r.json().get("id") if r.status_code == 200 else None

# 14. List versions
r = requests.get(f"{BASE}/projects/{pid}/versions", headers=headers, timeout=15)
log("list versions", r.status_code == 200 and len(r.json()) >= 1, f"status={r.status_code}")

# 15. Export
for fmt in ["yolo", "coco", "voc"]:
    r = requests.get(f"{BASE}/projects/{pid}/export?format={fmt}", headers=headers, timeout=30)
    ok = r.status_code == 200 and (r.headers.get("content-type","").startswith("application/") or r.headers.get("content-type","").startswith("application/zip"))
    is_zip = False
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        is_zip = len(zf.namelist()) > 0
    except Exception:
        pass
    log(f"export {fmt}", r.status_code == 200 and is_zip, f"status={r.status_code} zip={is_zip} bytes={len(r.content)}")

# 16. Train (simulated)
r = requests.post(f"{BASE}/projects/{pid}/train", headers=headers, json={"version_id": version_id, "epochs": 3, "model_arch": "yolov8n"}, timeout=60)
ok = r.status_code == 200
body = r.json() if ok else {}
log("train (simulated)", ok, f"status={r.status_code} keys={list(body.keys()) if isinstance(body,dict) else '?'} body={str(body)[:200]}")

# 17. List models
r = requests.get(f"{BASE}/projects/{pid}/models", headers=headers, timeout=15)
log("list models", r.status_code == 200 and len(r.json()) >= 1, f"status={r.status_code} count={len(r.json()) if r.status_code==200 else '?'}")

# 18. Delete image
if img_id:
    r = requests.delete(f"{BASE}/images/{img_id}", headers=headers, timeout=15)
    log("delete image", r.status_code == 200, f"status={r.status_code}")

# 19. Delete project
r = requests.delete(f"{BASE}/projects/{pid}", headers=headers, timeout=15)
log("delete project", r.status_code == 200, f"status={r.status_code}")

print("\n\n=== SUMMARY ===")
passed = sum(1 for r in results if r["ok"])
print(f"{passed}/{len(results)} passed")
for r in results:
    print(("[PASS]" if r["ok"] else "[FAIL]"), r["step"])

with open("/tmp/smoke_results.json","w") as f:
    json.dump(results, f, indent=2)
