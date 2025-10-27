import os
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel


API_TOKEN = get_api_token()
if API_TOKEN == "changeme":
    raise RuntimeError("API token not configured")
BASE_DIR = Path(os.environ.get("BASE_DIR", "/data")).resolve()
OUT_DIR = Path(os.environ.get("OUT_DIR", "/output")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="7zip API", version="1.0.0")

import os
import logging

def get_api_token(default="changeme") -> str:
    # 1) Prefer file, if provided
    token_path = os.getenv("API_TOKEN_FILE")
    if token_path and os.path.isfile(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token
            else:
                logging.warning("API_TOKEN_FILE is empty: %s", token_path)
        except Exception as e:
            logging.warning("Failed to read API_TOKEN_FILE %s: %s", token_path, e)

    # 2) Fallback to env var
    token = os.getenv("API_TOKEN")
    if token:
        return token.strip()

    # 3) Final fallback
    return default


def _require_auth(authorization: Optional[str]):
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

def _safe_path(p: str) -> Path:
    rp = Path(p).resolve()
    if not str(rp).startswith(str(BASE_DIR) + os.sep):
      # also allow BASE_DIR itself
      if str(rp) != str(BASE_DIR):
        raise HTTPException(status_code=400, detail="Path outside allowed base")
    return rp

def _safe_out(name: str) -> Path:
    # prevent directory traversal in output name
    out = (OUT_DIR / name).resolve()
    if not str(out).startswith(str(OUT_DIR) + os.sep) and str(out) != str(OUT_DIR):
        raise HTTPException(status_code=400, detail="Invalid output name")
    return out

def _run_7z(args: list):
    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"7z failed: {e}")

class ZipFolderReq(BaseModel):
    folder: str
    archive_name: str = "archive.zip"
    password: Optional[str] = None
    recursive: bool = True
    format: str = "zip"  # "7z" or "zip"

class UnzipReq(BaseModel):
    folder: str                 # directory under BASE_DIR where the archive resides
    archive_name: str           # e.g., "archive.zip" or "archive.7z"
    password: Optional[str] = None
    dest_dir: Optional[str] = None  # subdir under OUT_DIR; default derived from archive name
    overwrite: str = "skip"     # "skip" (-aos), "overwrite" (-aoa), or "rename" (-aou)

@app.get("/health")
def health():
    return {"status": "ok", "base_path": str(BASE_DIR), "out_path": str(OUT_DIR), "last_token_digits": str(API_TOKEN[-3:])}

@app.post("/zip-folder")
def zip_folder(req: ZipFolderReq, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    src = _safe_path(req.folder)
    if not src.exists() or not src.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    out = _safe_out(req.archive_name)
    tflag = "-t7z" if req.format.lower() == "7z" else "-tzip"
    args = ["7z", "a", tflag, str(out)]

    if req.password:
        # AES-256 + hide file names (7z only). For ZIP, -p works but -mhe applies to 7z.
        args += [f"-p{req.password}"]
        if req.format.lower() == "7z":
            args += ["-mhe=on"]

    # Add content
    if req.recursive:
        args += [str(src / "**" / "*")]
    else:
        args += [str(src / "*")]

    _run_7z(args)
    return FileResponse(str(out), filename=out.name,
                        media_type="application/x-7z-compressed" if req.format=="7z" else "application/zip")

@app.post("/unzip-archive")
def unzip_archive(req: UnzipReq, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)

    # Source folder and archive path must be inside BASE_DIR
    src_folder = _safe_path(req.folder)
    if not src_folder.exists() or not src_folder.is_dir():
        raise HTTPException(status_code=404, detail="Source folder not found")

    archive_path = (src_folder / req.archive_name).resolve()
    # Ensure the archive is within BASE_DIR
    if not str(archive_path).startswith(str(BASE_DIR) + os.sep) and str(archive_path) != str(BASE_DIR):
        raise HTTPException(status_code=400, detail="Archive path outside allowed base")

    if not archive_path.exists() or not archive_path.is_file():
        raise HTTPException(status_code=404, detail="Archive file not found")

    # Destination directory is always under OUT_DIR
    if req.dest_dir:
        # prevent traversal in provided dest_dir
        dest = (OUT_DIR / req.dest_dir).resolve()
    else:
        # default: /output/<archive_stem>/
        dest = (OUT_DIR / archive_path.stem).resolve()

    if not str(dest).startswith(str(OUT_DIR) + os.sep) and str(dest) != str(OUT_DIR):
        raise HTTPException(status_code=400, detail="Invalid destination path")

    dest.mkdir(parents=True, exist_ok=True)

    # Overwrite policy mapping
    ow_map = {"skip": "-aos", "overwrite": "-aoa", "rename": "-aou"}
    ow_flag = ow_map.get(req.overwrite.lower(), "-aos")

    # 7z extract command
    cmd = ["7z", "x", str(archive_path), f"-o{str(dest)}", ow_flag, "-y"]
    if req.password:
        cmd += [f"-p{req.password}"]

    _run_7z(cmd)

    # Return a small manifest
    try:
        # List extracted entries (non-recursive top level, to keep response small)
        entries = sorted([p.name for p in dest.iterdir()])
    except Exception:
        entries = []

    return {
        "status": "ok",
        "archive": str(archive_path),
        "extracted_to": str(dest),
        "entries_top_level": entries[:200]  # cap to avoid huge responses
    }