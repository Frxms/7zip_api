import os
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple

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
    rp = Path(p).resolve()
    # must be inside BASE_DIR (or BASE_DIR itself)
    if not str(rp).startswith(str(BASE_DIR) + os.sep) and str(rp) != str(BASE_DIR):
        raise HTTPException(status_code=400, detail="Path outside allowed base")
    return rp

def _safe_out(name: str) -> Path:
    # ensure output stays in OUT_DIR and no traversal
    out = (OUT_DIR / name).resolve()
    if not str(out).startswith(str(OUT_DIR) + os.sep) and str(out) != str(OUT_DIR):
        raise HTTPException(status_code=400, detail="Invalid output name")
    # create parent dirs just in case the name contains subfolders
    out.parent.mkdir(parents=True, exist_ok=True)
    return out

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

def _detect_single_root_dir(archive_path: Path) -> Optional[str]:
    """
    Inspect archive contents and return the single top-level component name
    if (and only if) every entry starts with the same first path segment.
    Uses `7z l -slt` for a parseable output.
    """
    try:
        res = subprocess.run(
            ["7z", "l", "-slt", str(archive_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logging.warning("Failed to list archive for root detection: %s", e)
        return None

    top_levels = set()
    for line in res.stdout.splitlines():
        # Lines look like: "Path = dir/file.txt"
        if line.startswith("Path = "):
            rel = line[len("Path = "):].strip()
            if not rel:
                continue
            # normalize separator to '/'
            rel = rel.replace("\\", "/")
            first = rel.split("/", 1)[0]
            top_levels.add(first)

            # If we ever see a top-level *file* (no '/'), that still counts as a top-level name,
            # but it means the archive has root files, so there is not a single root directory "container".
            # We'll keep collecting; decision happens after loop.

            if len(top_levels) > 1:
                # more than one top-level right away -> not a single root dir
                return None

    if len(top_levels) == 1:
        return next(iter(top_levels))
    return None


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
    dest_dir: Optional[str] = None  # subdir under OUT_DIR; default derived smartly
    overwrite: str = "skip"     # "skip" (-aos), "overwrite" (-aoa), or "rename" (-aou)


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

    src = _safe_path(req.folder)
    if not src.exists() or not src.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    out = _safe_out(req.archive_name)
    fmt = (req.format or "zip").lower()
    if fmt not in {"zip", "7z"}:
        raise HTTPException(status_code=400, detail="format must be 'zip' or '7z'")

    # 7z syntax: 7z a [options] <archive> <files...>
    # We run inside 'src' and add '.'; recursion with -r if requested.
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

    # Source folder (where the archive is) must be inside BASE_DIR
    src_folder = _safe_path(req.folder)
    if not src_folder.exists() or not src_folder.is_dir():
        raise HTTPException(status_code=404, detail="Source folder not found")

    archive_path = (src_folder / req.archive_name).resolve()

    # Ensure the archive is within BASE_DIR
    if not str(archive_path).startswith(str(BASE_DIR) + os.sep) and str(archive_path) != str(BASE_DIR):
        raise HTTPException(status_code=400, detail="Archive path outside allowed base")

    if not archive_path.exists() or not archive_path.is_file():
        raise HTTPException(status_code=404, detail="Archive file not found")

    # ---- NEW: smart destination to avoid double-nesting ----
    if req.dest_dir:
        dest = (OUT_DIR / req.dest_dir).resolve()
    else:
        embedded_root = _detect_single_root_dir(archive_path)
        archive_stem = archive_path.stem

        if embedded_root and embedded_root == archive_stem:
            # The archive already contains a single top-level folder that matches the stem.
            # Extract directly into OUT_DIR so we only get ONE folder (the embedded one).
            dest = OUT_DIR
        else:
            # Otherwise, create a folder named after the archive to contain the files.
            dest = (OUT_DIR / archive_stem).resolve()

    if not str(dest).startswith(str(OUT_DIR) + os.sep) and str(dest) != str(OUT_DIR):
        raise HTTPException(status_code=400, detail="Invalid destination path")

    dest.mkdir(parents=True, exist_ok=True)

    # Overwrite policy mapping
    ow_map = {"skip": "-aos", "overwrite": "-aoa", "rename": "-aou"}
    ow_flag = ow_map.get((req.overwrite or "skip").lower(), "-aos")

    # 7z extract: 7z x <archive> -o<dest> <overwrite-flag> -y [-p...]
    cmd = ["7z", "x", str(archive_path), f"-o{str(dest)}", ow_flag, "-y"]
    if req.password:
        cmd.append(f"-p{req.password}")

    logging.info("Running 7z (unzip) with args: %s", cmd)
    _run_7z(cmd)

    # Return a small manifest (top-level entries only)
    try:
        entries = sorted([p.name for p in dest.iterdir()])
    except Exception:
        entries = []

    return {
        "status": "ok",
        "archive": str(archive_path),
        "extracted_to": str(dest),
        "entries_top_level": entries[:200]
    }
