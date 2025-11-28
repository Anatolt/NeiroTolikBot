# Автоматическое развертывание из GitHub

Этот документ описывает настройку автоматической подгрузки изменений из GitHub и перезапуска бота.

## Компоненты

1. **webhook_server.py** - Flask сервер для приема webhook запросов от GitHub
2. **deploy.sh** - Скрипт для обновления кода и перезапуска бота
3. **webhook.service** - Systemd сервис для запуска webhook сервера

## Установка

### 1. Установка зависимостей

Убедитесь, что Flask установлен:

```bash
cd /root/tolikNeiroBot/NeiroTolikBot
pip install -r requirements.txt
```

### 2. Настройка переменных окружения

Добавьте в файл `.env` следующие переменные:

```bash
# Секретный ключ для верификации webhook (сгенерируйте случайную строку)
GITHUB_WEBHOOK_SECRET=ваш_секретный_ключ_здесь

# Порт для webhook сервера (по умолчанию 5000)
WEBHOOK_PORT=5000

# Хост для webhook сервера (по умолчанию 0.0.0.0)
WEBHOOK_HOST=0.0.0.0
```

**Важно:** Сгенерируйте безопасный секретный ключ:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Запуск webhook сервера

#### Вариант A: Через systemd (рекомендуется)

```bash
# Копируем сервис
sudo cp webhook.service /etc/systemd/system/

# Перезагружаем systemd
sudo systemctl daemon-reload

# Включаем автозапуск
sudo systemctl enable webhook.service

# Запускаем сервис
sudo systemctl start webhook.service

# Проверяем статус
sudo systemctl status webhook.service
```

#### Вариант B: Вручную (для тестирования)

```bash
cd /root/tolikNeiroBot/NeiroTolikBot
python webhook_server.py
```

### 4. Настройка GitHub Webhook

1. Перейдите в ваш репозиторий на GitHub
2. Откройте **Settings** → **Webhooks** → **Add webhook**
3. Заполните форму:
   - **Payload URL**: `http://ваш_сервер:5000/webhook`
     - Если сервер находится за NAT/файрволом, используйте ngrok или настройте проброс портов
   - **Content type**: `application/json`
   - **Secret**: тот же ключ, что вы указали в `GITHUB_WEBHOOK_SECRET`
   - **Which events**: выберите "Just the push event" или "Let me select individual events" → выберите "Pushes"
   - **Active**: отмечено
4. Нажмите **Add webhook**

### 5. Настройка доступа к webhook серверу

Если ваш сервер находится за файрволом или NAT, вам нужно настроить доступ:

#### Вариант A: Использование ngrok (для тестирования)

```bash
# Установите ngrok
# Затем запустите:
ngrok http 5000

# Используйте полученный URL в настройках GitHub webhook
```

#### Вариант B: Настройка файрвола (для продакшена)

```bash
# Откройте порт в файрволе (если используется ufw)
sudo ufw allow 5000/tcp

# Или настройте проброс портов на роутере
```

#### Вариант C: Использование reverse proxy (nginx)

Добавьте в конфигурацию nginx:

```nginx
server {
    listen 80;
    server_name ваш_домен.com;

    location /webhook {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Проверка работы

### 1. Проверка webhook сервера

```bash
# Проверка здоровья
curl http://localhost:5000/health

# Должен вернуть: {"status":"ok"}
```

### 2. Тестирование webhook

В настройках GitHub webhook нажмите "Recent Deliveries" и затем "Redeliver" для последнего события. Или сделайте тестовый push в репозиторий.

### 3. Просмотр логов

```bash
# Логи webhook сервера
sudo journalctl -u webhook.service -f

# Логи бота (если используется systemd)
sudo journalctl -u neirotolikbot.service -f

# Логи бота (если используется Docker)
docker-compose logs -f bot
```

## Как это работает

1. При push в ветку `main` или `master` GitHub отправляет POST запрос на ваш webhook сервер
2. Webhook сервер проверяет подпись запроса для безопасности
3. Запускается скрипт `deploy.sh`, который:
   - Выполняет `git pull` для получения последних изменений
   - Определяет способ запуска бота (Docker или systemd)
   - Перезапускает бота соответствующим способом

## Безопасность

- ✅ Webhook проверяет подпись запросов от GitHub
- ✅ Обрабатываются только push события в основную ветку
- ⚠️ Убедитесь, что webhook сервер доступен только из надежных источников
- ⚠️ Используйте HTTPS в продакшене (настройте SSL сертификат)

## Устранение неполадок

### Webhook не получает запросы

1. Проверьте, что сервер запущен: `sudo systemctl status webhook.service`
2. Проверьте логи: `sudo journalctl -u webhook.service -n 50`
3. Убедитесь, что порт открыт: `netstat -tlnp | grep 5000`
4. Проверьте настройки GitHub webhook (Recent Deliveries)

### Ошибка "Invalid signature"

- Убедитесь, что `GITHUB_WEBHOOK_SECRET` в `.env` совпадает с Secret в настройках GitHub webhook

### Ошибка при обновлении

- Проверьте права доступа к репозиторию
- Убедитесь, что git настроен правильно
- Проверьте логи: `sudo journalctl -u webhook.service -n 100`

## Ручной запуск обновления

Если нужно обновить вручную:

```bash
cd /root/tolikNeiroBot/NeiroTolikBot
bash deploy.sh
```

