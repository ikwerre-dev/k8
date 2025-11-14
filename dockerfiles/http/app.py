from fastapi import FastAPI, UploadFile, Form, File
import os, subprocess

app = FastAPI()

@app.post("/upload")
async def upload(foldername: str = Form(...), file: UploadFile = File(...)):
    upload_dir = "/app/uploads"
    os.makedirs(upload_dir, exist_ok=True)
    save_path = f"{upload_dir}/{foldername}.lz4"
    with open(save_path, "wb") as f:
        f.write(await file.read())
    build_path = f"{upload_dir}/{foldername}"
    os.makedirs(build_path, exist_ok=True)
    subprocess.run(["lz4", "-d", save_path, f"{build_path}/extracted.tar"], check=True)
    subprocess.run(["tar", "-xf", f"{build_path}/extracted.tar", "-C", build_path], check=True)
    os.remove(f"{build_path}/extracted.tar")
    os.remove(save_path)
    return {"status": "success", "folder": foldername}