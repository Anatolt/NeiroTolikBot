#!/usr/bin/env python3
"""
Webhook сервер для автоматического обновления бота из GitHub.
Принимает POST запросы от GitHub и запускает скрипт обновления.
"""
import os
import hmac
import hashlib
import subprocess
import logging
import threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

app = Flask(__name__)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Секретный ключ для верификации webhook (из .env)
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# Путь к скрипту обновления
DEPLOY_SCRIPT = os.path.join(os.path.dirname(__file__), "deploy.sh")

def run_deploy_script():
    """Запускает deploy.sh в отдельном потоке, чтобы не блокировать ответ GitHub."""
    try:
        env = os.environ.copy()
        # Явно прописываем PATH, чтобы systemd/cron не влияли на доступность bash/git
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
        result = subprocess.run(
            ["/usr/bin/bash", DEPLOY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=300,
            env=env
        )

        if result.returncode == 0:
            logger.info("Deployment successful")
            logger.info(f"Deploy output: {result.stdout}")
        else:
            logger.error(f"Deployment failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("Deployment script timeout")
    except Exception as e:
        logger.error(f"Error running deploy script: {str(e)}")


def verify_signature(payload_body, signature_header):
    """Проверяет подпись webhook от GitHub."""
    if not WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not set, skipping signature verification")
        return True
    
    if not signature_header:
        return False
    
    # GitHub отправляет подпись в формате "sha256=..."
    if not signature_header.startswith("sha256="):
        return False
    
    expected_signature = signature_header.split("=")[1]
    
    # Вычисляем ожидаемую подпись
    mac = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256
    )
    calculated_signature = mac.hexdigest()
    
    # Безопасное сравнение
    return hmac.compare_digest(expected_signature, calculated_signature)


@app.route("/webhook", methods=["POST"])
def github_webhook():
    """Обработчик webhook от GitHub."""
    try:
        # Получаем подпись из заголовков
        signature = request.headers.get("X-Hub-Signature-256")
        payload = request.get_data()
        
        # Проверяем подпись
        if not verify_signature(payload, signature):
            logger.warning("Invalid webhook signature")
            return jsonify({"error": "Invalid signature"}), 401
        
        # Парсим JSON payload
        event = request.get_json(silent=True)
        if event is None:
            logger.warning("Received webhook without JSON body")
            return jsonify({"error": "Invalid payload"}), 400
        
        # Проверяем тип события
        event_type = request.headers.get("X-GitHub-Event")
        
        if event_type == "push":
            # Проверяем, что это push в основную ветку (обычно main или master)
            ref = event.get("ref", "")
            if ref in ["refs/heads/main", "refs/heads/master"]:
                logger.info(f"Received push event to {ref}, starting deployment...")
                
                # Стартуем деплой асинхронно, чтобы GitHub не получал таймаут
                threading.Thread(target=run_deploy_script, daemon=True).start()
                return jsonify({
                    "status": "accepted",
                    "message": "Deployment started"
                }), 202
            else:
                logger.info(f"Ignoring push to {ref} (not main/master branch)")
                return jsonify({
                    "status": "ignored",
                    "message": f"Push to {ref} ignored (not main/master branch)"
                }), 200
        
        elif event_type == "ping":
            logger.info("Received ping event from GitHub")
            return jsonify({"status": "ok", "message": "Webhook is working"}), 200
        else:
            logger.info(f"Ignoring event type: {event_type}")
            return jsonify({
                "status": "ignored",
                "message": f"Event type {event_type} ignored"
            }), 200
            
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Error: {str(e)}"
        }), 500


@app.route("/health", methods=["GET"])
def health():
    """Проверка здоровья сервера."""
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("WEBHOOK_PORT", "5000"))
    host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    
    logger.info(f"Starting webhook server on {host}:{port}")
    app.run(host=host, port=port, debug=False)
