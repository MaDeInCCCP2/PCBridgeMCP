# PCBridge MCP

[English](#english) · [Русский](#русский)

## English

PCBridge connects an MCP client to tools running on your Windows PC. It can work with project files, inspect Git history, run builds, and interact with desktop apps or a dedicated browser. Available tools depend on the modules you enable. This README covers **PCBridge 1.7.0**.

> [!WARNING]
> Run this build on localhost only. The OAuth implementation in 1.7.0 can issue the main bridge token without the owner's approval. The bundled `START.bat` and `SETTINGS.bat` can start an ngrok tunnel. Use the manual startup below until the OAuth issue is fixed. If this build was publicly reachable, take the tunnel offline and rotate the bridge token.

### What it does

| Feature | What you can do |
| --- | --- |
| Files and Git | Search, read, and edit workspace files. Preview multi-file edits, detect conflicts with SHA-256, undo edits, and inspect Git status, diffs, and history. |
| Processes and builds | Run executables with explicit arguments, environment overrides, and stdin. Monitor background jobs, fetch incremental logs, and save checksummed copies of build artifacts. |
| Windows automation | Find and interact with controls through Windows UI Automation. Use window screenshots, OCR, and guarded mouse and keyboard input for custom interfaces. |
| Browser automation | Open a separate Chromium session with Playwright, inspect pages, interact with forms, diagnose failed requests, and save downloads. |
| Optional integrations | Enable modules for Blender, Roblox Studio, or Unreal Engine 5. The full bridge also includes a tray icon and a local control panel. |

### Local setup on Windows

Extract the 1.7.0 ZIP, then open PowerShell in the `PCBridge` folder that contains `server` and `VERSION.txt`. You need Python 3.12 with the Windows Python launcher. The first command creates a virtual environment.

```powershell
py -3.12 -m venv .venv
$bridgePython = '.\.venv\Scripts\python.exe'
& $bridgePython -m pip install -r .\server\requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'PCBridge dependency installation failed.' }

if (-not (Test-Path -LiteralPath '.\config.json')) {
    $bridgeToken = & $bridgePython -c 'import secrets; print(secrets.token_hex(32))'
    if ($LASTEXITCODE -ne 0 -or -not $bridgeToken) { throw 'Token generation failed.' }
    @{
        secret = $bridgeToken
        token_auth = $true
        port = 8765
        full_disk = $false
        enable_shell = $false
    } | ConvertTo-Json | Set-Content -LiteralPath '.\config.json' -Encoding UTF8
}
& $bridgePython .\server\server.py
```

This starts the MCP server in the foreground. It uses the `workspace` subfolder unless you set `workspace` in `config.json`. Keep the PowerShell window open while you use the bridge. Press **Ctrl+C** to stop it.

### Connect an MCP client

In a client running on the same PC, choose **Streamable HTTP** and enter:

- **URL** `http://127.0.0.1:8765/mcp` with the port from your configuration.
- **Authorization header** `Bearer <your token>`, using the value of `secret` in `config.json`. Keep that value private.
- **First call** `server_info` to check the connection and enabled modules.

For a URL-only local client, create `TOKEN.txt` with the same token, start `TOKEN_HTTP.bat` in a second window, and use `http://127.0.0.1:8770/<token>/mcp`. This URL contains a secret. Keep it on the same PC.

### Enable tools and run tests

Set the relevant flags in `config.json`, then restart the server.

| Feature | Settings |
| --- | --- |
| Screenshots and UI Automation inspection | `"enable_screen": true` |
| Desktop mouse and keyboard input | `"enable_screen": true`, `"enable_input": true` |
| Playwright browser | `"enable_browser": true`, `"enable_internet": true`. Enable `enable_input` for browser actions. Install Chromium with `& $bridgePython -m playwright install chromium`. |
| Optional DXGI capture | `"enable_dxgi_capture": true` with screen capture enabled. Install the `windows-capture` package. |
| Local vision adapter | `"enable_vision_adapter": true` and `vision_adapter_path` pointing to your adapter script. |
| Shell and process tools | `"enable_shell": true` |
| Read-only access | `"read_only": true` |

Run `RUN_TESTS.bat` from the installation folder. See the [release notes and downloads](https://github.com/MaDeInCCCP2/PCBridgeMCP/releases) for changes between versions.

---

## Русский

PCBridge подключает MCP-клиент к инструментам, которые выполняются на вашем Windows-ПК. Через мост можно работать с файлами проекта, просматривать историю Git, запускать сборки и управлять приложениями или отдельным браузером. Набор инструментов зависит от включённых модулей. Эта инструкция относится к **PCBridge 1.7.0**.

> [!WARNING]
> Запускайте эту сборку только на localhost. Реализация OAuth в 1.7.0 может выдать основной токен моста без разрешения владельца. Скрипты `START.bat` и `SETTINGS.bat` могут запустить туннель ngrok. До исправления OAuth используйте ручной запуск ниже. Если сборка была доступна из интернета, отключите туннель и смените токен моста.

### Возможности

| Функция | Что можно делать |
| --- | --- |
| Файлы и Git | Искать, читать и редактировать файлы рабочей папки. Просматривать правки нескольких файлов перед применением, обнаруживать конфликты по SHA-256, откатывать изменения и смотреть состояние, различия и историю Git. |
| Процессы и сборки | Запускать программы с явными аргументами, отдельными переменными окружения и stdin. Следить за фоновыми задачами, получать новые строки логов и сохранять копии артефактов с контрольными суммами. |
| Автоматизация Windows | Искать элементы и взаимодействовать с ними через Windows UI Automation. Для нестандартных интерфейсов доступны снимки окон, OCR и ввод мышью и клавиатурой с проверками. |
| Автоматизация браузера | Запускать отдельный Chromium через Playwright, читать страницы, работать с формами, диагностировать сбои запросов и сохранять загрузки. |
| Дополнительные интеграции | Подключать модули Blender, Roblox Studio и Unreal Engine 5. Полный режим работы моста также включает значок в трее и локальную панель управления. |

### Локальная установка на Windows

Распакуйте ZIP-архив 1.7.0 и откройте PowerShell в папке `PCBridge`, где находятся `server` и `VERSION.txt`. Нужен Python 3.12 с лаунчером Python для Windows. Первая команда создаёт виртуальное окружение.

```powershell
py -3.12 -m venv .venv
$bridgePython = '.\.venv\Scripts\python.exe'
& $bridgePython -m pip install -r .\server\requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'PCBridge dependency installation failed.' }

if (-not (Test-Path -LiteralPath '.\config.json')) {
    $bridgeToken = & $bridgePython -c 'import secrets; print(secrets.token_hex(32))'
    if ($LASTEXITCODE -ne 0 -or -not $bridgeToken) { throw 'Token generation failed.' }
    @{
        secret = $bridgeToken
        token_auth = $true
        port = 8765
        full_disk = $false
        enable_shell = $false
    } | ConvertTo-Json | Set-Content -LiteralPath '.\config.json' -Encoding UTF8
}
& $bridgePython .\server\server.py
```

MCP-сервер работает в открытом окне PowerShell. По умолчанию рабочая папка — `workspace`. Другую папку можно указать в поле `workspace` файла `config.json`. Для остановки нажмите **Ctrl+C**.

### Подключение MCP-клиента

В клиенте на том же ПК выберите **Streamable HTTP** и укажите:

- **URL** `http://127.0.0.1:8765/mcp` с портом из вашей конфигурации.
- **Заголовок Authorization** `Bearer <ваш токен>`. Возьмите токен из поля `secret` файла `config.json` и храните его в секрете.
- **Первый вызов** `server_info`, чтобы проверить подключение и список включённых модулей.

Если локальный клиент принимает только URL, создайте `TOKEN.txt` с тем же токеном, запустите `TOKEN_HTTP.bat` в отдельном окне и укажите `http://127.0.0.1:8770/<token>/mcp`. Этот адрес содержит секрет и предназначен для использования на том же ПК.

### Включение инструментов и запуск тестов

Укажите нужные параметры в `config.json` и перезапустите сервер.

| Функция | Параметры |
| --- | --- |
| Снимки экрана и чтение интерфейса через UI Automation | `"enable_screen": true` |
| Ввод мышью и клавиатурой | `"enable_screen": true`, `"enable_input": true` |
| Браузер Playwright | `"enable_browser": true`, `"enable_internet": true`. Для действий в браузере включите `enable_input`. Установите Chromium командой `& $bridgePython -m playwright install chromium`. |
| Дополнительный захват через DXGI | `"enable_dxgi_capture": true` при включённом захвате экрана. Установите пакет `windows-capture`. |
| Локальный адаптер распознавания | `"enable_vision_adapter": true`. В `vision_adapter_path` укажите путь к скрипту адаптера. |
| Командная строка и запуск процессов | `"enable_shell": true` |
| Доступ только на чтение | `"read_only": true` |

Запустите `RUN_TESTS.bat` из папки установки. Изменения между версиями перечислены на [странице релизов GitHub](https://github.com/MaDeInCCCP2/PCBridgeMCP/releases).
