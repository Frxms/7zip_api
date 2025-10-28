import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging

# ---- Logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---- Token loading with file > env > default fallback ----
def get_api_token(default="changeme") -> str:
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

    token = os.getenv("API_TOKEN")
    if token:
        return token.strip()

    return default

API_TOKEN = get_api_token()
if API_TOKEN == "changeme":
    raise RuntimeError("API token not configured")

BASE_DIR = Path(os.environ.get("BASE_DIR", "/data")).resolve()
OUT_DIR = Path(os.environ.get("OUT_DIR", "/output")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="7zip API", version="1.0.0")


# ---- Helpers ----
def _require_auth(authorization: Optional[str]):
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

def _safe_path(p: str) -> Path:
    """
    Resolve a path safely under BASE_DIR.
    - Relative input is treated as relative to BASE_DIR.
    - Absolute input must still resolve under BASE_DIR.
    """
    cand = Path(p)
    rp = (BASE_DIR / cand).resolve() if not cand.is_absolute() else cand.resolve()
    if not str(rp).startswith(str(BASE_DIR) + os.sep) and str(rp) != str(BASE_DIR):
        raise HTTPException(status_code=400, detail=f"Path outside allowed base: {rp}")
    return rp

def _safe_out(name: str) -> Path:
    """
    Ensure an output path remains within OUT_DIR and prevent traversal.
    Creates parent directories if needed.
    """
    out = (OUT_DIR / name).resolve()
    if not str(out).startswith(str(OUT_DIR) + os.sep) and str(out) != str(OUT_DIR):
        raise HTTPException(status_code=400, detail=f"Invalid output name: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out

def _ensure_under_out(p: Path):
    if not str(p).startswith(str(OUT_DIR) + os.sep) and str(p) != str(OUT_DIR):
        raise HTTPException(status_code=400, detail=f"Destination escapes OUT_DIR: {p}")

def _unique_path(base: Path) -> Path:
    """Return a unique path by appending a short suffix if the path exists."""
    if not base.exists():
        return base
    suffix = "-" + uuid.uuid4().hex[:6]
    return base.with_name(base.name + suffix)

def _run_7z(args: list, cwd: Optional[Path] = None):
    try:
        res = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
        if res.stdout:
            logging.info("7z stdout (trunc): %s", res.stdout[:1000])
    except subprocess.CalledProcessError as e:
        logging.error("7z failed. stdout=%s stderr=%s", e.stdout, e.stderr)
        raise HTTPException(
            status_code=500,
            detail=f"7z failed: {(e.stderr or e.stdout or str(e)).strip()}",
        )


# ---- Schemas ----
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
    dest_dir: Optional[str] = None  # final folder name under OUT_DIR (optional)
    overwrite: str = "skip"     # "skip", "overwrite", or "rename"


# ---- Routes ----
@app.get("/health")
def health():
    # Only expose last 3 chars to avoid leaking the token
    return {
        "status": "ok",
        "base_path": str(BASE_DIR),
        "out_path": str(OUT_DIR),
        "last_token_digits": API_TOKEN[-3:] if API_TOKEN else None,
    }


@app.post("/zip-folder")
def zip_folder(req: ZipFolderReq, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)

    logging.info("zip request: folder=%r archive_name=%r recursive=%r format=%r",
                 req.folder, req.archive_name, req.recursive, req.format)

    src = _safe_path(req.folder)
    if not src.exists() or not src.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {src}")

    out = _safe_out(req.archive_name)
    fmt = (req.format or "zip").lower()
    if fmt not in {"zip", "7z"}:
        raise HTTPException(status_code=400, detail="format must be 'zip' or '7z'")

    # 7z syntax: 7z a [options] <archive> <files...>
    # Run inside 'src' and add '.'; recursion with -r if requested.
    options = []
    tflag = "-t7z" if fmt == "7z" else "-tzip"
    options.append(tflag)

    if req.password:
        # -p works for both formats; -mhe=on only applicable to 7z
        options.append(f"-p{req.password}")
        if fmt == "7z":
            options.append("-mhe=on")

    if req.recursive:
        options.append("-r")

    args = ["7z", "a"] + options + [str(out), "."]

    logging.info("Running 7z (zip) with args: %s (cwd=%s)", args, src)
    _run_7z(args, cwd=src)

    media_type = "application/x-7z-compressed" if fmt == "7z" else "application/zip"
    return FileResponse(str(out), filename=out.name, media_type=media_type)


@app.post("/unzip-archive")
def unzip_archive(req: UnzipReq, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization)
    logging.info(
        "unzip request: folder=%r archive_name=%r dest_dir=%r overwrite=%r",
        req.folder, req.archive_name, req.dest_dir, req.overwrite
    )

    # Source folder (where the archive is) must be inside BASE_DIR
    src_folder = _safe_path(req.folder)
    if not src_folder.exists() or not src_folder.is_dir():
        raise HTTPException(status_code=404, detail=f"Source folder not found: {src_folder}")

    archive_path = (src_folder / req.archive_name).resolve()
    if not str(archive_path).startswith(str(BASE_DIR) + os.sep) and str(archive_path) != str(BASE_DIR):
        raise HTTPException(status_code=400, detail=f"Archive path outside allowed base: {archive_path}")
    if not archive_path.exists() or not archive_path.is_file():
        raise HTTPException(status_code=404, detail=f"Archive file not found: {archive_path}")

    # ---- Decide final target folder (single level) ----
    archive_stem = archive_path.stem  # e.g. "invoices" from "invoices.zip"
    final_dir = (OUT_DIR / (req.dest_dir or archive_stem)).resolve()
    _ensure_under_out(final_dir)

    # Overwrite policy at folder level
    mode = (req.overwrite or "skip").lower()
    if final_dir.exists():
        if mode == "overwrite":
            shutil.rmtree(final_dir)
        elif mode == "rename":
            final_dir = _unique_path(final_dir)
        else:  # skip
            raise HTTPException(status_code=409, detail=f"Destination exists: {final_dir}")

    # ---- Extract to temp dir first ----
    temp_dir = (OUT_DIR / f".extract-{archive_stem}-{uuid.uuid4().hex[:8]}").resolve()
    _ensure_under_out(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    cmd = ["7z", "x", str(archive_path), f"-o{str(temp_dir)}", "-y"]
    if req.password:
        cmd.append(f"-p{req.password}")

    logging.info("Running 7z (unzip) with args: %s", cmd)
    try:
        _run_7z(cmd)
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    # ---- Normalize to a single folder level ----
    # If temp contains exactly one directory, use it as final_dir; else wrap all items into final_dir.
    entries = list(temp_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        # Move that single directory to final_dir (no extra nesting)
        entries[0].replace(final_dir)
    else:
        final_dir.mkdir(parents=True, exist_ok=False)
        for p in entries:
            shutil.move(str(p), str(final_dir))

    # Cleanup temp directory
    shutil.rmtree(temp_dir, ignore_errors=True)

    # Return a small manifest (top-level entries only)
    try:
        top_entries = sorted([p.name for p in final_dir.iterdir()])
    except Exception:
        top_entries = []

    return {
        "status": "ok",
        "archive": str(archive_path),
        "extracted_to": str(final_dir),
        "entries_top_level": top_entries[:200]
    }
