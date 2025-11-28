#!/bin/bash
# Скрипт для автоматического обновления и перезапуска бота

set -e  # Остановка при ошибке

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Логирование
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2
}

warning() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}

# Определяем директорию проекта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SCRIPT_DIR - это NeiroTolikBot, там находится .git
cd "$SCRIPT_DIR"

log "Starting deployment..."
log "Project directory: $SCRIPT_DIR"

# Проверяем, используется ли Docker
# Сначала проверяем systemd, так как это приоритетнее
if systemctl is-active --quiet neirotolikbot.service 2>/dev/null; then
    log "Detected systemd service (priority)"
    # Продолжим ниже к проверке systemd
elif command -v docker-compose &> /dev/null || command -v docker &> /dev/null; then
    if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        log "Detected Docker Compose setup"
        
        # Переходим в директорию с docker-compose.yml
        cd "$SCRIPT_DIR"
        
        # Обновляем код из GitHub
        log "Pulling latest changes from GitHub..."
        git pull origin main || git pull origin master || {
            error "Failed to pull changes from GitHub"
            exit 1
        }
        
        # Пересобираем и перезапускаем контейнер
        log "Rebuilding and restarting Docker container..."
        if command -v docker-compose &> /dev/null; then
            docker-compose down
            docker-compose build --no-cache
            docker-compose up -d
        elif docker compose version &> /dev/null 2>&1; then
            # Используем docker compose (новая версия, плагин)
            docker compose down
            docker compose build --no-cache
            docker compose up -d
        else
            error "Neither docker-compose nor docker compose found"
            exit 1
        fi
        
        log "Docker container restarted successfully"
        exit 0
    fi
fi

# Проверяем, используется ли systemd
if systemctl is-active --quiet neirotolikbot.service 2>/dev/null; then
    log "Detected systemd service"
    
    # Обновляем код из GitHub
    log "Pulling latest changes from GitHub..."
    git pull origin main || git pull origin master || {
        error "Failed to pull changes from GitHub"
        exit 1
    }
    
    # Перезапускаем systemd сервис
    log "Restarting systemd service..."
    systemctl restart neirotolikbot.service
    
    # Проверяем статус
    sleep 2
    if systemctl is-active --quiet neirotolikbot.service; then
        log "Systemd service restarted successfully"
    else
        error "Failed to restart systemd service"
        systemctl status neirotolikbot.service
        exit 1
    fi
    exit 0
fi

# Если ничего не найдено, просто обновляем код
warning "No Docker or systemd service detected, only pulling changes..."
git pull origin main || git pull origin master || {
    error "Failed to pull changes from GitHub"
    exit 1
}

log "Code updated successfully (manual restart required)"

