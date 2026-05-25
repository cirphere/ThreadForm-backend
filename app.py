"""
API Server — 외부 노출 FastAPI 서버 (:8000).

모바일 앱과 통신하는 얇은 API 레이어.
분석 로직은 Analysis Worker(:8001)에 위임한다.
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
WORKER_URL = "http://127.0.0.1:8001"

app = FastAPI(title="TreadForm API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _ensure_dirs():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / "renders").mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / "reports").mkdir(parents=True, exist_ok=True)


@app.post("/analyze")
async def analyze(file: UploadFile):
    print("===== /analyze 요청 수신 =====")
    print(f"  filename: {file.filename}")
    print(f"  content_type: {file.content_type}")
    print(f"  size (headers): {file.size}")

    if not file.filename or not file.filename.lower().endswith(".mp4"):
        print(f"  [거부] mp4가 아님 — filename={file.filename}")
        raise HTTPException(status_code=400, detail="mp4 파일만 업로드 가능합니다.")

    file_id = uuid.uuid4().hex[:12]
    save_path = UPLOADS_DIR / f"{file_id}.mp4"

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    file_size = save_path.stat().st_size
    print(f"  저장 완료: {save_path} ({file_size} bytes)")

    if file_size == 0:
        print("  [에러] 파일 크기가 0 bytes")
        raise HTTPException(status_code=400, detail="빈 파일입니다.")

    print(f"  워커 호출: {WORKER_URL}/run")
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            resp = await client.post(
                f"{WORKER_URL}/run",
                json={
                    "video_path": str(save_path),
                    "output_dir": str(STORAGE_DIR),
                },
            )
        except httpx.ConnectError:
            print(f"  [에러] 워커 연결 실패: {WORKER_URL}")
            raise HTTPException(status_code=503, detail="분석 워커가 실행 중이 아닙니다.")

    print(f"  워커 응답: status={resp.status_code}")
    print(f"  워커 body (500자): {resp.text[:500]}")

    if resp.status_code == 422:
        detail = resp.json().get("detail", {})
        print(f"  [경고] 워커 검증 실패: {detail}")
        raise HTTPException(status_code=400, detail=detail)
    if resp.status_code != 200:
        print(f"  [에러] 워커 에러: status={resp.status_code} body={resp.text[:300]}")
        raise HTTPException(status_code=502, detail="분석 중 오류가 발생했습니다.")

    data = resp.json()
    analysis_id = data["analysis_result"]["analysis_id"]
    print(f"===== 분석 완료: {analysis_id} =====")

    return {
        **data["analysis_result"],
        "coach_message": data["coach_message"],
        "rendered_video_url": f"/results/{analysis_id}/video",
        "csv_report_url": f"/results/{analysis_id}/csv",
    }


@app.get("/results/{analysis_id}/video")
async def get_video(analysis_id: str):
    path = STORAGE_DIR / "renders" / f"{analysis_id}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="렌더링 영상을 찾을 수 없습니다.")
    return FileResponse(path, media_type="video/mp4", filename=f"{analysis_id}.mp4")


@app.get("/results/{analysis_id}/csv")
async def get_csv(analysis_id: str):
    path = STORAGE_DIR / "reports" / f"{analysis_id}.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="CSV 리포트를 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/csv", filename=f"{analysis_id}.csv")


@app.get("/health")
async def health():
    worker_ok = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{WORKER_URL}/health")
            worker_ok = r.status_code == 200
    except Exception:
        pass

    return {
        "status": "ok",
        "service": "api",
        "worker": "ok" if worker_ok else "unavailable",
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
