#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from config import BOT_CONFIG
from services.generation import categorize_models, fetch_imagerouter_models, fetch_models_data
from services.memory import get_miniapp_settings, init_db, set_miniapp_settings

load_dotenv()

BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["MINI_APP_URL"] = os.getenv("MINI_APP_URL", BOT_CONFIG.get("MINI_APP_URL"))

SESSION_SECRET = (
    os.getenv("MINI_APP_SESSION_SECRET")
    or BOT_CONFIG.get("TELEGRAM_BOT_TOKEN")
    or "mini-app-dev-secret"
)
SESSION_TTL = int(os.getenv("MINI_APP_SESSION_TTL", "86400"))
MAX_INITDATA_AGE = int(os.getenv("MINI_APP_MAX_INITDATA_AGE", "86400"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
init_db()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("utf-8"))


def _sign_value(value: str) -> str:
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(signature)


def issue_session_token(user_id: str) -> str:
    now = int(time.time())
    payload = {
        "uid": str(user_id),
        "iat": now,
        "exp": now + SESSION_TTL,
    }
    encoded_payload = _b64url_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = _sign_value(encoded_payload)
    return f"{encoded_payload}.{signature}"


def verify_session_token(token: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    encoded_payload, signature = token.split(".", 1)
    expected = _sign_value(encoded_payload)
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload_raw = _b64url_decode(encoded_payload)
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None

    exp = int(payload.get("exp", 0))
    if exp <= int(time.time()):
        return None
    if not payload.get("uid"):
        return None
    return payload


def parse_init_data(init_data: str) -> dict[str, str]:
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    return {key: value for key, value in pairs}


def verify_telegram_init_data(init_data: str) -> dict[str, Any] | None:
    bot_token = BOT_CONFIG.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is required for Mini App auth")
        return None

    params = parse_init_data(init_data)
    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={params[key]}" for key in sorted(params.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(received_hash, expected_hash):
        return None

    auth_date_raw = params.get("auth_date")
    try:
        auth_date = int(auth_date_raw) if auth_date_raw else 0
    except ValueError:
        return None

    now = int(time.time())
    if auth_date <= 0 or (now - auth_date) > MAX_INITDATA_AGE:
        return None

    user_data: dict[str, Any] = {}
    user_raw = params.get("user")
    if user_raw:
        try:
            parsed_user = json.loads(user_raw)
            if isinstance(parsed_user, dict):
                user_data = parsed_user
        except json.JSONDecodeError:
            user_data = {}

    user_id = user_data.get("id")
    if not user_id:
        return None

    return {
        "user": user_data,
        "params": params,
    }


def _session_user_id() -> str | None:
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = request.headers.get("X-MiniApp-Token", "").strip()
    payload = verify_session_token(token)
    if not payload:
        return None
    return str(payload["uid"])


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


async def _collect_available_models() -> dict[str, list[str]]:
    text_models: list[str] = []
    try:
        models_data = await fetch_models_data()
        if models_data:
            categories = categorize_models(models_data)
            for key in ("free", "large_context", "specialized", "paid"):
                for model in categories.get(key, []):
                    model_id = model.get("id")
                    if model_id:
                        text_models.append(model_id)
    except Exception as exc:
        logger.warning("Failed to fetch text models for Mini App: %s", exc)

    if not text_models:
        text_models = [BOT_CONFIG.get("DEFAULT_MODEL")]

    voice_models = list(BOT_CONFIG.get("VOICE_MODELS", []))

    image_models = list(BOT_CONFIG.get("PIAPI_IMAGE_MODELS", []) or [])
    try:
        image_models.extend(await fetch_imagerouter_models())
    except Exception as exc:
        logger.warning("Failed to fetch image models for Mini App: %s", exc)

    image_models.extend(BOT_CONFIG.get("IMAGE_MODELS", []) or [])

    return {
        "text": _dedupe([m for m in text_models if m]),
        "voice": _dedupe([m for m in voice_models if m]),
        "image": _dedupe([m for m in image_models if m]),
    }


def _available_models_sync() -> dict[str, list[str]]:
    return asyncio.run(_collect_available_models())


def _features_payload() -> list[dict[str, str]]:
    return [
        {
            "title": "Текстовые ответы",
            "description": "Диалог с LLM, память контекста и выбор модели ответа.",
        },
        {
            "title": "Голосовые сообщения",
            "description": "Распознавание голосовых и ответы бота по транскрипции.",
        },
        {
            "title": "Генерация изображений",
            "description": "Создание картинок по текстовому описанию.",
        },
        {
            "title": "Консилиум моделей",
            "description": "Сравнение ответов нескольких моделей на один вопрос.",
        },
        {
            "title": "Роутинг запросов",
            "description": "Алгоритмический или LLM-роутинг в зависимости от задачи.",
        },
        {
            "title": "Гибкие настройки",
            "description": "Персональный выбор моделей для текста, голоса и картинок.",
        },
    ]


@app.get("/")
@app.get("/miniapp")
@app.get("/miniapp/")
def miniapp_index():
    html = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>НейроТолик Mini App</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #f3f0e7;
      --card: #fffdf7;
      --ink: #1f1a14;
      --muted: #64574a;
      --accent: #0f766e;
      --accent-2: #f59e0b;
      --line: #ded4c4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "Trebuchet MS", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% -20%, #f9e7b7 0, transparent 45%),
        radial-gradient(circle at 110% 10%, #b8ece8 0, transparent 40%),
        var(--bg);
    }
    .wrap {
      max-width: 720px;
      margin: 0 auto;
      padding: 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 6px 24px rgba(31, 26, 20, 0.06);
    }
    h1 {
      margin: 0 0 10px;
      font-size: 22px;
      line-height: 1.2;
    }
    .muted {
      color: var(--muted);
      font-size: 14px;
      margin: 0 0 14px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }
    .btn {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      color: var(--ink);
      padding: 12px;
      text-align: left;
      font-size: 15px;
      cursor: pointer;
    }
    .btn.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .btn.warn {
      background: var(--accent-2);
      color: #1f1a14;
      border-color: var(--accent-2);
    }
    .screen { display: none; }
    .screen.active { display: block; }
    .stack { display: grid; gap: 10px; }
    .field { display: grid; gap: 6px; }
    label { font-size: 13px; color: var(--muted); }
    select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      font-size: 14px;
      background: #fff;
    }
    .notice {
      font-size: 13px;
      color: var(--muted);
      background: #fff;
      border: 1px dashed var(--line);
      border-radius: 10px;
      padding: 10px;
    }
    ul { margin: 0; padding-left: 18px; }
    li { margin: 6px 0; }
    .status { font-size: 13px; color: var(--muted); min-height: 18px; }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }
    .back {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      padding: 6px 10px;
      font-size: 13px;
      cursor: pointer;
    }
    @media (max-width: 480px) {
      h1 { font-size: 20px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div id="screen-home" class="screen active">
        <h1>НейроТолик Mini App</h1>
        <p class="muted">Управление функциями бота и персональными настройками моделей.</p>
        <div class="grid">
          <button class="btn primary" data-open="features">Перечень функционала бота</button>
          <button class="btn warn" data-open="pay">Заплатить денег</button>
          <button class="btn" data-open="settings">Настройки</button>
        </div>
      </div>

      <div id="screen-features" class="screen">
        <div class="topbar">
          <h1>Функционал бота</h1>
          <button class="back" data-open="home">Назад</button>
        </div>
        <ul id="features-list"></ul>
      </div>

      <div id="screen-pay" class="screen">
        <div class="topbar">
          <h1>Оплата</h1>
          <button class="back" data-open="home">Назад</button>
        </div>
        <div class="notice">
          Спасибо большое, что вы это нажали. Пока этот функционал не работает, но скоро появится.
          Будут очень большие лимиты на использование бота.
        </div>
      </div>

      <div id="screen-settings" class="screen">
        <div class="topbar">
          <h1>Настройки</h1>
          <button class="back" data-open="home">Назад</button>
        </div>
        <div class="stack">
          <div class="field">
            <label for="textModel">Модель текстовых ответов</label>
            <select id="textModel"></select>
          </div>
          <div class="field">
            <label for="voiceModel">Модель распознавания голоса</label>
            <select id="voiceModel"></select>
          </div>
          <div class="field">
            <label for="imageModel">Модель генерации картинок</label>
            <select id="imageModel"></select>
          </div>
          <button id="saveSettings" class="btn primary">Сохранить настройки</button>
          <div id="settingsStatus" class="status"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) {
      tg.ready();
      tg.expand();
    }

    const screens = {
      home: document.getElementById("screen-home"),
      features: document.getElementById("screen-features"),
      pay: document.getElementById("screen-pay"),
      settings: document.getElementById("screen-settings"),
    };

    function openScreen(name) {
      Object.entries(screens).forEach(([key, node]) => {
        if (!node) return;
        if (key === name) node.classList.add("active");
        else node.classList.remove("active");
      });
    }

    document.querySelectorAll("[data-open]").forEach((btn) => {
      btn.addEventListener("click", () => openScreen(btn.getAttribute("data-open")));
    });

    const settingsStatus = document.getElementById("settingsStatus");
    const textModel = document.getElementById("textModel");
    const voiceModel = document.getElementById("voiceModel");
    const imageModel = document.getElementById("imageModel");

    let sessionToken = "";

    function setStatus(text) {
      settingsStatus.textContent = text || "";
    }

    function fillSelect(selectNode, options, selected) {
      selectNode.innerHTML = "";
      (options || []).forEach((value) => {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = value;
        if (value === selected) opt.selected = true;
        selectNode.appendChild(opt);
      });
    }

    async function authMiniApp() {
      const initData = tg ? tg.initData : "";
      if (!initData) {
        throw new Error("Mini App auth недоступна: нет initData.");
      }
      const res = await fetch("/miniapp/api/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Ошибка авторизации Mini App");
      }
      sessionToken = data.token;
    }

    async function loadFeatures() {
      const res = await fetch("/miniapp/api/features");
      const data = await res.json();
      const list = document.getElementById("features-list");
      list.innerHTML = "";
      (data.features || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${item.title}: ${item.description}`;
        list.appendChild(li);
      });
    }

    async function loadSettings() {
      setStatus("Загружаю настройки...");
      const res = await fetch("/miniapp/api/settings", {
        headers: { Authorization: `Bearer ${sessionToken}` },
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Не удалось загрузить настройки");
      }

      fillSelect(textModel, data.options.text_models, data.settings.text_model);
      fillSelect(voiceModel, data.options.voice_models, data.settings.voice_model);
      fillSelect(imageModel, data.options.image_models, data.settings.image_model);
      setStatus("Настройки загружены");
    }

    async function saveSettings() {
      setStatus("Сохраняю...");
      const payload = {
        text_model: textModel.value,
        voice_model: voiceModel.value,
        image_model: imageModel.value,
      };
      const res = await fetch("/miniapp/api/settings", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${sessionToken}`,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Не удалось сохранить настройки");
      }
      setStatus("Сохранено");
      if (tg) tg.HapticFeedback.notificationOccurred("success");
    }

    document.getElementById("saveSettings").addEventListener("click", async () => {
      try {
        await saveSettings();
      } catch (err) {
        setStatus(err.message || String(err));
      }
    });

    (async () => {
      try {
        await loadFeatures();
        await authMiniApp();
        await loadSettings();
      } catch (err) {
        setStatus(err.message || String(err));
      }
    })();
  </script>
</body>
</html>
"""
    return html


@app.post("/miniapp/api/auth")
def miniapp_auth():
    payload = request.get_json(silent=True) or {}
    init_data = str(payload.get("initData") or "").strip()
    if not init_data:
        return jsonify({"ok": False, "error": "Missing initData"}), 400

    verified = verify_telegram_init_data(init_data)
    if not verified:
        return jsonify({"ok": False, "error": "Invalid initData"}), 401

    user_id = str(verified["user"]["id"])
    token = issue_session_token(user_id)

    return jsonify({
        "ok": True,
        "token": token,
        "user_id": user_id,
    })


@app.get("/miniapp/api/features")
def miniapp_features():
    return jsonify({"ok": True, "features": _features_payload()})


@app.get("/miniapp/api/settings")
def miniapp_get_settings():
    user_id = _session_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    options = _available_models_sync()
    defaults = {
        "text_model": BOT_CONFIG.get("DEFAULT_MODEL"),
        "voice_model": BOT_CONFIG.get("VOICE_MODEL"),
        "image_model": BOT_CONFIG.get("IMAGE_GENERATION", {}).get("MODEL"),
    }

    settings = get_miniapp_settings(user_id)
    result = {
        "text_model": settings.get("text_model") or defaults["text_model"],
        "voice_model": settings.get("voice_model") or defaults["voice_model"],
        "image_model": settings.get("image_model") or defaults["image_model"],
    }

    if result["text_model"] and result["text_model"] not in options["text"]:
        options["text"].append(result["text_model"])
    if result["voice_model"] and result["voice_model"] not in options["voice"]:
        options["voice"].append(result["voice_model"])
    if result["image_model"] and result["image_model"] not in options["image"]:
        options["image"].append(result["image_model"])

    return jsonify({
        "ok": True,
        "settings": result,
        "options": {
            "text_models": options["text"],
            "voice_models": options["voice"],
            "image_models": options["image"],
        },
    })


@app.put("/miniapp/api/settings")
def miniapp_update_settings():
    user_id = _session_user_id()
    if not user_id:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    text_model = payload.get("text_model")
    voice_model = payload.get("voice_model")
    image_model = payload.get("image_model")

    options = _available_models_sync()

    if text_model and text_model not in options["text"]:
        return jsonify({"ok": False, "error": "Unknown text model"}), 400
    if voice_model and voice_model not in options["voice"]:
        return jsonify({"ok": False, "error": "Unknown voice model"}), 400
    if image_model and image_model not in options["image"]:
        return jsonify({"ok": False, "error": "Unknown image model"}), 400

    set_miniapp_settings(
        user_id=str(user_id),
        text_model=text_model,
        voice_model=voice_model,
        image_model=image_model,
    )

    return jsonify({"ok": True})


@app.get("/miniapp/health")
def miniapp_health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = os.getenv("MINI_APP_HOST", "0.0.0.0")
    port = int(os.getenv("MINI_APP_PORT", "8080"))
    logger.info("Starting Mini App server on %s:%s", host, port)
    app.run(host=host, port=port, debug=False)
