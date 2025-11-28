# Быстрый старт: Автоматическое обновление из GitHub

## Шаг 1: Установка зависимостей

```bash
cd /root/tolikNeiroBot/NeiroTolikBot
pip install -r requirements.txt
```

## Шаг 2: Настройка .env

Добавьте в файл `.env`:

```bash
# Сгенерируйте секретный ключ:
# python3 -c "import secrets; print(secrets.token_urlsafe(32))"
GITHUB_WEBHOOK_SECRET=ваш_сгенерированный_ключ
WEBHOOK_PORT=5000
```

## Шаг 3: Запуск webhook сервера

```bash
# Установка systemd сервиса
sudo cp webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webhook.service
sudo systemctl start webhook.service

# Проверка
sudo systemctl status webhook.service
```

## Шаг 4: Настройка GitHub Webhook

1. GitHub → Ваш репозиторий → Settings → Webhooks → Add webhook
2. **Payload URL**: `http://ваш_IP_или_домен:5000/webhook`
3. **Content type**: `application/json`
4. **Secret**: тот же ключ, что в `GITHUB_WEBHOOK_SECRET`
5. **Events**: выберите "Just the push event"
6. Сохраните

## Шаг 5: Проверка

```bash
# Проверка здоровья сервера
curl http://localhost:5000/health

# Просмотр логов
sudo journalctl -u webhook.service -f
```

## Готово!

Теперь при каждом push в ветку `main` или `master` бот автоматически обновится и перезапустится.

## Важно для доступа извне

Если сервер за файрволом, откройте порт:
```bash
sudo ufw allow 5000/tcp
```

Или используйте ngrok для тестирования:
```bash
ngrok http 5000
```

