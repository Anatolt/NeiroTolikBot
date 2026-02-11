import os
import subprocess
import tempfile
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel
from starlette.responses import Response

app = FastAPI()

DEFAULT_MODEL = os.getenv("PIPER_MODEL", "/models/ru_RU-irina-medium.onnx")
DEFAULT_CONFIG = os.getenv("PIPER_CONFIG", "/models/ru_RU-irina-medium.onnx.json")


class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = None


def _resolve_model(voice: Optional[str]) -> tuple[str, str]:
    if not voice:
        return DEFAULT_MODEL, DEFAULT_CONFIG
    candidate_model = f"/models/{voice}.onnx"
    candidate_config = f"/models/{voice}.onnx.json"
    if os.path.exists(candidate_model) and os.path.exists(candidate_config):
        return candidate_model, candidate_config
    return DEFAULT_MODEL, DEFAULT_CONFIG


@app.post("/tts")
async def tts(request: TTSRequest) -> Response:
    text = (request.text or "").strip()
    if not text:
        return Response(content=b"", media_type="audio/wav", status_code=400)

    model_path, config_path = _resolve_model(request.voice)

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp_path = tmp.name

        proc = subprocess.run(
            [
                "piper",
                "--model",
                model_path,
                "--config",
                config_path,
                "--output_file",
                tmp_path,
            ],
            input=text.encode("utf-8"),
            check=False,
        )
        if proc.returncode != 0:
            return Response(content=b"", media_type="audio/wav", status_code=500)

        with open(tmp_path, "rb") as fh:
            audio_bytes = fh.read()
        return Response(content=audio_bytes, media_type="audio/wav")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
