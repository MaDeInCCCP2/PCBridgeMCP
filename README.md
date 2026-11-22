# PCBridge MCP

[English](#english) · [Русский](#русский)

## English

PCBridge connects an MCP client to tools running on your Windows PC. It can work with project files, inspect Git history, run builds, and interact with desktop apps or a dedicated browser. Available tools depend on the modules you enable. This README covers **PCBridge 2.0.0**.

PCBridge uses MCP Python SDK v2 and requires Python 3.11 or later. It serves both the legacy Streamable HTTP handshake and the 2026-07-28 protocol on the existing endpoint. On upgrade, the Windows setup preserves an older Python virtual environment and creates a supported one.

If you exposed an earlier 1.7.0 build through a public tunnel, rotate the owner token. Reconnect OAuth clients that registered with that build.

For an upgrade, keep the existing configuration folder and OAuth database. OAuth clients still use the same MCP URL and authentication. Jobs carried over from older versions remain available to the owner. Win32 Desktop remains experimental and requires explicitly approved applications.

### What it does

| Feature | What you can do |
| --- | --- |
| Files and Git | Search, read, and edit workspace files. Preview multi-file edits, detect conflicts with SHA-256, undo edits, and inspect Git status, diffs, and history. |
| Processes and builds | Run executables with explicit arguments, environment overrides, and stdin. Monitor background jobs, fetch incremental logs, and save checksummed copies of build artifacts. |
| Windows automation | Find and interact with controls through Windows UI Automation. Use window screenshots, OCR, and guarded mouse and keyboard input for custom interfaces. |
| Browser automation | Open a separate Chromium session with Playwright, inspect pages, interact with forms, diagnose failed requests, and save downloads. |
| Optional integrations | Enable modules for Blender, Roblox Studio, or Unreal Engine 5. The full bridge also includes a tray icon and a local control panel. |

### Local setup on Windows

Extract the 2.0.0 ZIP, then open PowerShell in the `PCBridge` folder that contains `server` and `VERSION.txt`. You need Python 3.12 with the Windows Python launcher. The first command creates a virtual environment.

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

### OAuth and remote connections

The OAuth consent page asks for the owner token from `TOKEN.txt` on the bridge's own address. Approval issues a separate token scoped to that MCP endpoint for one hour. OAuth clients use an exact registered redirect URI and PKCE S256.

The supervised ngrok tunnel registers its current HTTPS origin automatically. For a separately managed reverse proxy, set `"public_origin": "https://bridge.example"` in `config.json` and preserve the original Host header. PCBridge ignores forwarded host and scheme headers.

Run `python tools/revoke_oauth_client.py` locally to list client IDs. Pass an ID to revoke that registration and its tokens. Direct bearer authentication with the owner token remains available. For existing owner-managed machine clients, the client credentials grant accepts client ID `pcbridge` and the owner token.

### Experimental Win32 Desktop sessions

This mode runs allowlisted Windows 10 and 11 applications on a separate, non-input Win32 desktop. Its UI Automation tools only see windows in that session's job. Closing the session ends the job and its application processes. The desktop shares the Windows account, files and network. Application compatibility and isolation from the user's activity are still experimental.

Set `enable_background_desktop` to `true` in the local bridge configuration and list approved programs in `background_desktop_apps`. Owners can use all entries. Add explicit OAuth client IDs under `clients` to grant access to specific entries. Enable `enable_input` to expose UIA actions.

```json
{
  "enable_background_desktop": true,
  "enable_input": true,
  "background_desktop_apps": {
    "example": {
      "executable": "C:\\Tools\\Example.exe",
      "args": [],
      "clients": []
    }
  }
}
```

Use `desktop_apps`, `desktop_session_open`, `desktop_windows` and `desktop_snapshot`. Pass the snapshot ID, element ref and a unique `operation_id` to `desktop_action`. Reusing an operation ID returns the previous receipt. Finish with `desktop_session_close` or close it from the Work section of the local panel.

Use `desktop_lease_acquire` when an automation client needs exclusive input for a bounded period. Send the returned `fence` to `desktop_action_leased` and to `desktop_lease_release`. Once enabled for a session, all subsequent input requires a current fence. Renew with `renew_fence`. Read operations remain available when the lease expires. `desktop_session_recover` starts a new instance after an owned session exits or is lost. Repeating recovery returns the same replacement, which has its own snapshots and operation IDs.

Use `desktop_wait` with a window title to wait for a window at startup. Add `text` or `role` to wait for a visible element, or pass `present: false` to check that an element has disappeared from a complete UI Automation tree. The tool uses the same session and returns fresh refs. It is available while session input is paused.

In the `desktop_action` response, `action_executed` records whether the action ran. `verification.status` records the separate result of the check. It is `verified`, `not_observed`, `unavailable`, or `not_requested`. A check that fails or cannot run returns `ok: false` even if the action executed. The same operation ID returns that outcome again. Use a new `desktop_snapshot` to decide what to do next.

Use `desktop_operation(session_id, operation_id)` to check a submitted action with read-only access. It returns `running` or `finished`. A finished response contains the recorded `receipt`, including its own `ok` and `outcome` fields. The query accepts just the two IDs. Receipt status remains available on disk for 24 hours after a bridge restart or session closure. The stored receipt contains verification status without snapshots or input data. Add `desktop_operation` to the OAuth client's tool allowlist if it uses an explicit profile.

In the local **Work** tab, expand **Actions** under a Windows desktop session to see its latest ten operations. The list shows the operation ID, start time and verification status. Input values and window snapshots stay out of the list.

Use **Pause input** or **Resume input** in the Windows session row to control that session locally. Pausing refuses new `desktop_action` requests with `desktop_input_paused`. Snapshots and action receipts remain available. An action already accepted can finish. The panel reports the change and `desktop_sessions` exposes it as `input_paused`.

When every process in the desktop job exits, the worker releases the desktop and the session shows **Exited**. Exited sessions and sessions whose worker crashed free their active slot automatically. The bridge keeps the last 16 such sessions and their recorded action receipts in memory. You can remove individual entries from the Work tab.

### Approved desktop peers

`desktop_peer_handoff(session_id, peer, operation_id)` starts a fresh approved application on a second PCBridge. It passes only the target app alias. The source remains open, and the destination has separate snapshots and action receipts. A successful receipt retains the destination session ID even if the source bridge restarts. For a repeat call, use the same operation ID and source session. Use `desktop_peer_operation(peer, operation_id)` to read the retained result even after closing the source session. If the outcome is unknown, inspect the destination before opening another copy.

Configure the destination with an approved app and a dedicated OAuth client allowed to call `desktop_session_open`. On the source, install a locally granted portable plugin with a Streamable HTTP component pointing to the destination, pin its content hash and grant its `desktop_session_open` tool to the source caller. Map the component's `Authorization` to an environment variable containing the dedicated destination bearer value. Non-loopback endpoints with credentials need HTTPS. Configure the alias on the source:

```json
{
  "desktop_peers": {
    "lab": {
      "plugin": "lab-bridge",
      "server": "computer",
      "clients": ["<source OAuth client ID>"],
      "apps": {"local-editor": "lab-editor"}
    }
  }
}
```

Use `computer_isolation_status` to check Child Sessions and Sandbox support. On a host with Child Sessions enabled, the local experimental viewer `powershell.exe -NoProfile -STA -File tools/child_session.ps1 -Connect` opens the Windows loopback child session. Use `-CheckControl` to validate its ActiveX control without connecting. The viewer does not enable Windows features or ask for credentials. To automate from another bridge, start a separate configured PCBridge inside the child session and expose its approved apps through a desktop peer.

Run `python tools/benchmark_computer_use.py --scenario win32-desktop --runs 5` on an unlocked Windows desktop to exercise the WinForms and WPF fixtures. It samples the foreground window, cursor position and clipboard sequence without reading clipboard contents. The report counts runs with indistinguishable activity or unavailable signals as inconclusive. The exit codes are 0 for a clean run, 1 for a test failure and 2 for an inconclusive run.

Use `--scenario win32-chromium` to run a local HTML fixture with the full Playwright Chromium build, or `--scenario win32-electron --executable C:\Tools\Electron\electron.exe` with a local Electron distribution. Both use a temporary profile and a separate Win32 desktop. The report verifies a single UI Automation button action, checks red and green framebuffer pixels when WebGL is available, and records the renderer separately from the desktop interference measurements.

To build the WinUI 3 fixture, run `dotnet build tests/fixtures/winui/PCBridge.WinUI.Fixture.csproj -c Release -o <temp-dir>`. Pass the built EXE to `--scenario win32-winui --executable <temp-dir>\PCBridge.WinUI.Fixture.exe` to verify the native button action separately.

Use `--scenario win32-active-input --seconds 15` to verify clicks while you type into your own window. Keep that window in the foreground and the mouse still. The report records only the number of fixture clicks that overlapped a change in the Windows last-input timestamp.

Use `--scenario win32-lock-transition --seconds 25` to test the same background session before, during, and after a manual lock. Wait for the fixture's prompt, lock Windows, leave it locked for at least five seconds, and unlock it yourself. The benchmark listens for lock and unlock notifications. It returns inconclusive if it cannot observe the needed transitions. Use `--scenario win32-monitor-scales` to see whether the currently connected monitors expose different scaling factors. Pass the WinUI fixture executable to `--scenario win32-winui-dpi` to move it between two monitors. The test reports the actual window DPI on both monitors and is inconclusive for mixed DPI when the values match.

### OAuth client profiles

An optional `client_profiles` object in `config.json` maps OAuth client IDs to profiles. Read the IDs in **Security** in the local panel. Restart the bridge to load changes.

```json
{
  "client_profiles": {
    "pcbridge-0123456789abcdef0123456789abcdef": {
      "tools": ["server_info", "file_info", "skills_list", "skill_read"],
      "skills": ["project-guide"],
      "plugins": [],
      "job_max_active": 2,
      "browser_max_active": 2,
      "desktop_max_active": 1
    }
  }
}
```

A supplied list grants only the named entries. An empty list grants none. An omitted list inherits the enabled tool or extension catalog. Bundled skill IDs use `plugin-name/skill-name` and also require that plugin to be visible. External plugin calls also require a pinned `plugin_mcp_grants` entry. The owner token sees the enabled tools and extensions defined by local bridge settings.

### Background work

The **Work** tab shows active jobs and recent outcomes, including jobs recovered after a bridge restart. A job whose result could not be recovered appears as **Outcome unknown**. You can stop a running job and its process tree from the panel. The job history contains identifiers, timestamps and status metadata. The browser section displays open-tab and retained-snapshot counts and can close a selected Chromium context.

Each OAuth client can inspect, read output from, and stop its own jobs. The owner can manage jobs from every client, including jobs created before 2.0. The local setting `job_max_active_per_client` defaults to 8 concurrent jobs for each OAuth client. An optional profile can reduce `browser_max_active` (default 4) or `desktop_max_active` (default 2). Set a limit to zero to prevent that client from opening sessions.

### UI Automation action receipts

Use `ui_action_once` with a fresh UI Automation snapshot, element ref and a unique operation ID to prevent duplicate action dispatch. `ui_operation` reads the stored result for 24 hours, including after a bridge restart. Retrying the same request returns only its receipt. If the process or worker stopped before confirming the operation, inspect the application before issuing a new action. The receipt contains status and verification fields and omits the typed value, expected text and window snapshot. `ui_run_steps_once` provides the same guarantee for a sequence and reports how many steps completed or sent input before a stop.

### Developer diagnostics

Open **Diagnostics** in the local panel to browse enabled MCP tools and inspect their input and output schemas. The recent-call table shows up to 250 entries from the structured log, including durations, outcome codes and correlation IDs. **Download JSON** exports these entries without client identifiers, call arguments, results, or raw logs.

The local inspector can run `computer_capabilities` and `computer_plan`, including from a recent-call row. Enter fresh JSON arguments and select the run button. For example, `computer_plan` accepts `{"target":"browser","intent":"observe"}` to show available backends. Each run follows pause and read-only settings and records only call metadata.

### Browser sessions and extensions

Use `browser_session_open` for a fresh Chromium context. It defaults to headless and keeps separate cookies and storage. Use the returned page ID with existing browser tools, `browser_sessions` to inspect your contexts, and `browser_session_close` to release one. Visible Chromium and desktop tools may change focus.

For a browser action that must run at most once, use `browser_action_once` with the explicit session ID, page ID, snapshot ID, ref and a unique operation ID. Repeating the same request returns its receipt without issuing input again. `browser_operation` reads the receipt while work is pending and after completion. Receipts survive bridge restarts and are retained on disk for 24 hours. They include status without input values or page content. An interrupted action with no recorded result is reported as unknown. An unknown result calls for a fresh observation. Browser requests use a bounded queue that rotates between client identities.

Run `python tools/benchmark_computer_use.py --scenario browser-concurrency` to exercise 50 client tasks in groups of up to 16 concurrent Chromium contexts, the configured worker maximum. The benchmark uses a loopback fixture to check cookies, local storage, page-handle isolation and cleanup on cancellation. It reports the observed peak and p50/p95 task duration. Use `--parallel` to adjust the group size.

Run `python tools/benchmark_computer_use.py --scenario browser-interference --runs 5` on Windows to measure headless automation against sampled foreground, cursor and clipboard signals. Foreground activity that cannot be attributed to the fixture or absent signal readings make a trial inconclusive. Run `--scenario windows-isolation` with the same tool to detect the Sandbox launchers and query Child Sessions without changing Windows settings. On a Windows Sandbox host, `python tools/windows_sandbox.py prepare test.wsb --fixture` creates a local test profile. Open it in Windows Sandbox or use `start` with a new output filename. With the modern `wsb.exe` CLI, `start` records the guest ID beside the profile and `stop test.wsb` closes only that recorded guest. The legacy launcher relies on closing the Sandbox window. The fixture option shares only a generated read-only folder with the test script. The profile disables network and device redirection and enables Protected Client.

If a response is lost, call `computer_receipt(operation_id)` to find your retained UI Automation, browser and Win32 Desktop action receipts across sessions. Pass `backend` or `session_id` to narrow the lookup. Each match includes its backend, session ID when applicable, state and receipt. The lookup sends no input. It reports partial results if a backend is unavailable or if more than 20 sessions share the ID in one backend. Profiles with explicit tool allowlists need `computer_receipt` and the corresponding `ui_operation`, `browser_operation` or `desktop_operation` permission.

Use `computer_plan(target="win32_desktop")` to plan work on an explicitly approved background application. The optional `computer_plan_history` ranks permitted backends using the caller’s recent tool outcomes and falls back to the deterministic order with fewer than five observations per candidate. `computer_sessions` lists the client's browser and Win32 Desktop sessions together. Window refs, UI Automation snapshots, frame IDs and operation receipts are scoped to the calling client. `computer_verify` reports `changed_bounds` and clustered `changed_regions` in capture pixel coordinates for comparable frames, using the requested pixel threshold. Highly fragmented changes use the enclosing bounds.

Compare trusted local vision adapters with `python tools/benchmark_vision.py cases.json --adapter detector.py`. The manifest is a JSON array such as `[{"image":"dialog.jpg","query":"Save","boxes":[[10,20,80,30]]}]`. Repeat `--adapter` to compare adapters on the same corpus. The report includes top-1/top-5 hits and p50/p95 request time.

Use `python tools/manage_extensions.py import PATH` with a skill directory, a portable plugin directory, a local checkout, or a ZIP. Review the package and enable it in the tray panel's Extensions section or run `python tools/manage_extensions.py enable skill NAME`. Connected MCP clients can then discover the enabled skills with `skills_list` and read them with `skill_read`. The plugin catalog shows discovered MCP entries and unsupported adapters. Enabling a plugin makes its bundled skills visible.

To allow an MCP server from an enabled plugin, add a local grant in `config.json`. Pin the imported SHA-256 and name the tools for each client. Use `owner` for direct owner-token access or the OAuth client ID from the Security page. Target fields in `credentials` map to environment-variable names. They are sent as process environment variables for stdio and HTTP headers for Streamable HTTP.

```json
{
  "plugin_mcp_grants": {
    "example.plugin": {
      "service": {
        "sha256": "<imported package SHA-256>",
        "clients": {"owner": ["lookup"]},
        "credentials": {"Authorization": "PCBRIDGE_EXAMPLE_AUTHORIZATION"}
      }
    }
  }
}
```

Use `plugin_tools` and `plugin_call` after restarting PCBridge. Stdio uses the shell module and HTTP uses the internet module. Set a unique operation ID for every new call. The same ID reports the recorded outcome without replaying the request. After repeated transport failures PCBridge returns `plugin_backoff` with a retry delay for that client. The next request after the delay opens a new connection.

### Command confirmation

If you are asking PCBridge to run a potentially risky action through `run_command` or `run_process`, pass an optional `risk_explanation` in the user's language. In 600 characters or fewer, explain why it is needed, what it affects and what could be lost. Leave out credentials. The local confirmation labels this text as supplied by the agent and displays the bridge's own reason separately. PCBridge evaluates its rules and any enabled local AI check independently. Enter, Esc, or a timeout cancels the launch.

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

PCBridge подключает MCP-клиент к инструментам, которые выполняются на вашем Windows-ПК. Через мост можно работать с файлами проекта, просматривать историю Git, запускать сборки и управлять приложениями или отдельным браузером. Набор инструментов зависит от включённых модулей. Эта инструкция относится к **PCBridge 2.0.0**.

PCBridge использует MCP Python SDK v2 и требует Python 3.11 или новее. По прежнему адресу работают клиенты со старым рукопожатием Streamable HTTP и клиенты протокола 2026-07-28. При обновлении установщик Windows сохраняет окружение со старым Python и создаёт совместимое.

Если предыдущая сборка 1.7.0 была доступна через публичный туннель, смените токен владельца. Подключите заново зарегистрированные в той сборке OAuth-клиенты.

### Возможности

| Функция | Что можно делать |
| --- | --- |
| Файлы и Git | Искать, читать и редактировать файлы рабочей папки. Просматривать правки нескольких файлов перед применением, обнаруживать конфликты по SHA-256, откатывать изменения и смотреть состояние, различия и историю Git. |
| Процессы и сборки | Запускать программы с явными аргументами, отдельными переменными окружения и stdin. Следить за фоновыми задачами, получать новые строки логов и сохранять копии артефактов с контрольными суммами. |
| Автоматизация Windows | Искать элементы и взаимодействовать с ними через Windows UI Automation. Для нестандартных интерфейсов доступны снимки окон, OCR и ввод мышью и клавиатурой с проверками. |
| Автоматизация браузера | Запускать отдельный Chromium через Playwright, читать страницы, работать с формами, диагностировать сбои запросов и сохранять загрузки. |
| Дополнительные интеграции | Подключать модули Blender, Roblox Studio и Unreal Engine 5. Полный режим работы моста также включает значок в трее и локальную панель управления. |

Для однократного выполнения действия UI Automation вызовите `ui_action_once` со свежим снимком, ссылкой на элемент и уникальным ID операции. `ui_operation` показывает квитанцию до 24 часов, в том числе после перезапуска PCBridge. Если исход неизвестен, проверьте состояние приложения перед новым действием. Квитанция хранит статус и результат проверки без введённого значения, ожидаемого текста или снимка окна. `ui_run_steps_once` применяет ту же защиту к сценарию и показывает, сколько шагов завершилось или успело отправить ввод.

Во вкладке **Работа** видны фоновые задачи и их результаты после перезапуска моста. Владелец может остановить задачу вместе с дочерними процессами и закрыть выбранную сессию Chromium. Каждый OAuth-клиент видит только свои задачи. Настройка `job_max_active_per_client` задаёт лимит одновременных задач на клиента. По умолчанию лимит равен 8.

В `config.json` можно задать `client_profiles` по ID из раздела **Безопасность**. Профиль задаёт списки доступных инструментов MCP (`tools`), навыков (`skills`) и плагинов (`plugins`), а также индивидуальные лимиты задач (`job_max_active`), сессий браузера (`browser_max_active`, по умолчанию 4) и Win32 Desktop (`desktop_max_active`, по умолчанию 2). Ноль запрещает клиенту открывать сессии этого типа. Пустой список закрывает доступ ко всему списку. Изменения вступают в силу после перезапуска.

В разделе **Диагностика** можно просматривать схемы MCP и повторно запускать `computer_capabilities` и `computer_plan`. Кнопка **Подготовить повтор** в журнале открывает форму инструмента. Аргументы вводятся заново в JSON. Для `computer_plan` можно указать `{"target":"browser","intent":"observe"}`, чтобы увидеть подходящие способы наблюдения за браузером.

### Локальная установка на Windows

Распакуйте ZIP-архив 2.0.0 и откройте PowerShell в папке `PCBridge`, где находятся `server` и `VERSION.txt`. Нужен Python 3.12 с лаунчером Python для Windows. Первая команда создаёт виртуальное окружение.

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

### OAuth и удалённые подключения

Страница согласия OAuth запрашивает токен владельца из `TOKEN.txt` по адресу самого моста. После подтверждения клиент получает отдельный токен на один час, привязанный к адресу MCP. Перенаправление должно точно совпадать с зарегистрированным URI. Требуется PKCE S256.

PCBridge автоматически учитывает HTTPS-адрес туннеля ngrok, запущенного через START.bat. Для собственного прокси укажите `"public_origin": "https://bridge.example"` в `config.json` и сохраняйте исходный заголовок Host. Заголовки с переадресованными хостом или схемой игнорируются.

Команда `python tools/revoke_oauth_client.py` показывает ID клиентов. Передайте ID, чтобы отозвать регистрацию и все её токены. Можно по-прежнему подключаться с токеном владельца в заголовке Bearer. Для сервисного подключения используйте client ID `pcbridge` и токен владельца.

### Экспериментальные фоновые сессии Windows

Отдельный Win32 Desktop позволяет запустить разрешённое приложение и управлять его окнами через UI Automation. В локальном конфиге задайте `enable_background_desktop`, `enable_input` и список `background_desktop_apps` по примеру выше. Клиент OAuth получает доступ к приложению, если его ID указан в `clients`.

Начните с `desktop_apps` и `desktop_session_open`, затем используйте `desktop_windows`, `desktop_snapshot` и `desktop_action`. Закройте приложение через `desktop_session_close` или раздел «Работа» в панели. Закрытие завершает процессы сессии. Приложения работают с учётными данными текущего пользователя. Совместимость приложений и отсутствие побочных эффектов проверяются отдельно.

Вызов `desktop_wait` позволяет дождаться окна по заголовку или элемента внутри него по тексту и роли. Укажите `present: false`, чтобы проверить исчезновение элемента из полного дерева UI Automation. Инструмент возвращает новый снимок и доступен на паузе ввода.

В ответе `desktop_action` поле `action_executed` показывает, было ли выполнено действие. Поле `verification.status` показывает результат проверки. Если ожидаемый результат не наблюдался или проверка недоступна, ответ содержит `ok: false` даже после выполнения действия. Повтор с тем же ID вернёт сохранённый результат. Для проверки текущего состояния используйте `desktop_snapshot`.

Если ответ потерялся, вызовите `computer_receipt(operation_id)`. Поиск найдёт ваши сохранённые квитанции UI Automation, браузера и Win32 Desktop. Параметры `backend` и `session_id` сужают поиск. В ответе указаны подсистема, ID сессии при наличии, состояние и квитанция. При ошибке хранилища или более чем 20 совпадениях в одной подсистеме соответствующая отметка выводится в `backends`. Поиск доступен в режиме чтения. Для профиля с явным списком инструментов нужны разрешения на `computer_receipt` и на чтение квитанций выбранной подсистемы.

Чтобы узнать статус уже отправленного действия, вызовите `desktop_operation` с ID сессии и операции. В режиме чтения доступны статусы `running` и `finished`. При завершении ответ содержит результат в поле `receipt`. Статус сохраняется на диске на 24 часа и доступен после закрытия сессии или перезапуска PCBridge. В квитанции нет снимков и введённых данных.

Во вкладке **Работа** раскройте **Действия** под сессией Windows, чтобы увидеть до десяти последних операций. В списке доступны ID, время запуска и статус проверки.

Кнопки **Приостановить ввод** и **Возобновить ввод** настраивают отдельную сессию. Во время паузы новые вызовы `desktop_action` получают ошибку `desktop_input_paused`. Снимки и журнал доступны. Уже принятое действие может завершиться.

Когда завершается последний процесс сессии, PCBridge освобождает отдельный рабочий стол. Завершившиеся и аварийно остановленные сессии освобождают место для новых запусков автоматически. В памяти хранятся до 16 таких сессий вместе с результатами действий. Удалить отдельную запись можно в разделе «Работа».

### Передача работы между экземплярами PCBridge

`desktop_peer_handoff(session_id, peer, operation_id)` запускает одобренное приложение на другом PCBridge. Исходная сессия остаётся открытой, а принимающая сторона выдаёт новый ID сессии и собственные ссылки на элементы. Квитанция сохраняет ID назначения. Для повторного запроса укажите тот же ID операции и исходную сессию. `desktop_peer_operation(peer, operation_id)` запрашивает квитанцию даже после закрытия исходной сессии. При неизвестном результате проверьте принимающий экземпляр перед повторным запуском.

На принимающем компьютере заведите отдельного OAuth-клиента с доступом к `desktop_session_open` и нужному приложению. На исходном настройте компонент Streamable HTTP в локально разрешённом плагине, закрепите его SHA-256 и выдайте исходному клиенту доступ к `desktop_session_open`. Заголовок `Authorization` берётся из переменной окружения с учётными данными принимающего клиента. В `desktop_peers` задайте псевдоним узла, плагин, компонент, список исходных клиентов и соответствия приложений по примеру выше.

`computer_isolation_status` показывает состояние Child Sessions и Sandbox. Если Child Sessions включены в Windows, команда `powershell.exe -NoProfile -STA -File tools/child_session.ps1 -Connect` открывает локальное экспериментальное окно дочерней сессии. Режим `-CheckControl` проверяет компонент ActiveX без подключения. Для автоматизации можно запустить отдельно настроенный PCBridge внутри дочерней сессии и предоставить его приложения через `desktop_peers`.

Команда `python tools/benchmark_computer_use.py --scenario win32-desktop --runs 5` запускает по пять проверок WinForms и WPF на разблокированном рабочем столе Windows. Она измеряет изменения фокуса, положения курсора и счётчика буфера обмена. Если сигналы недоступны или изменения нельзя связать с тестируемым приложением, результат отмечается как неопределённый.

Режим `--scenario win32-chromium` запускает локальную HTML-страницу в полной сборке Playwright Chromium. Для Electron используйте `--scenario win32-electron --executable C:\Tools\Electron\electron.exe`. Оба режима используют временный профиль и отдельный рабочий стол Win32. Отчёт проверяет однократное нажатие через UI Automation, считывает красный и зелёный пиксели из буфера кадра при наличии WebGL и показывает тип рендерера и измерения взаимодействия с основным рабочим столом.

Для WinUI 3 соберите тестовое приложение командой `dotnet build tests/fixtures/winui/PCBridge.WinUI.Fixture.csproj -c Release -o <temp-dir>` и передайте EXE в `--scenario win32-winui --executable <temp-dir>\PCBridge.WinUI.Fixture.exe`. Этот сценарий проверяет кнопку WinUI через UI Automation.

Для ограниченного по времени доступа к вводу вызовите `desktop_lease_acquire` и передайте полученный `fence` в `desktop_action_leased`. Для продления передайте `renew_fence`, для завершения вызовите `desktop_lease_release`. После первого получения права оно требуется для любого ввода в этой сессии. `desktop_session_recover` открывает новый экземпляр приложения после выхода или сбоя. Повтор вызова возвращает уже созданную сессию, а действия из предыдущей сессии не повторяются.

Режим `--scenario win32-active-input --seconds 15` проверяет фоновые нажатия, пока вы печатаете в своём окне. Оставьте его активным и не двигайте мышь. Отчёт показывает только число операций, которые совпали с изменением отметки времени последнего ввода Windows.

Режим `--scenario win32-lock-transition --seconds 25` проверяет ту же фоновую сессию до, во время и после блокировки. После приглашения заблокируйте Windows, подождите минимум пять секунд и разблокируйте её самостоятельно. При отсутствии нужных уведомлений проверка вернёт неопределённый результат. Режим `--scenario win32-monitor-scales` покажет, есть ли среди подключённых мониторов разные масштабы. Режим `--scenario win32-winui-dpi` с тем же EXE перемещает окно между двумя мониторами, сообщает фактический DPI окна на обоих и помечает одинаковые значения как неопределённый результат проверки разных DPI.

### Диагностика для разработки

Во вкладке «Диагностика» можно найти активный MCP-инструмент и посмотреть схемы его аргументов и результатов. Таблица последних вызовов показывает до 250 записей с длительностью, результатом и идентификатором вызова. «Скачать JSON» выгружает эти записи без идентификаторов клиентов, аргументов, результатов инструментов и сырых логов.

### Браузерные сессии и расширения

`browser_session_open` создаёт отдельный Chromium-контекст со своими cookies и хранилищем. По умолчанию он работает без окна. Используйте выданный ID страницы в привычных инструментах браузера, `browser_sessions` для списка сессий и `browser_session_close` для завершения. Оконный Chromium и инструменты рабочего стола могут переводить фокус.

Для действия, которое должно выполниться максимум один раз, вызовите `browser_action_once` с ID явной сессии, страницы и снимка, ссылкой на элемент и уникальным ID операции. Повтор запроса возвращает квитанцию. `browser_operation` показывает её до и после завершения. Квитанции хранятся на диске 24 часа и доступны после перезапуска PCBridge. Они содержат состояние без введённых значений и содержимого страницы. Действие, для которого не записан итог, получает неизвестный исход. При неизвестном исходе заново снимите состояние страницы. Очередь браузера ограничена и чередует запросы разных клиентов.

Команда `python tools/benchmark_computer_use.py --scenario browser-concurrency` выполняет 50 клиентских задач группами до 16 одновременных контекстов Chromium. Локальная тестовая страница проверяет изоляцию cookies, localStorage и ссылок на страницы. Сценарий также проверяет освобождение контекста при отмене и завершение Chromium после закрытия последнего контекста. Отчёт показывает максимум одновременно открытых контекстов и длительность p50/p95.

Команда `python tools/benchmark_computer_use.py --scenario browser-interference --runs 5` измеряет фокус, положение курсора и счётчик буфера обмена при работе Chromium без окна. Если показания недоступны или изменения нельзя связать с тестом, результат считается неопределённым. Режим `--scenario windows-isolation` того же инструмента проверяет наличие средств запуска Sandbox и статус Child Sessions без изменения настроек Windows. На машине с Windows Sandbox команда `python tools/windows_sandbox.py prepare test.wsb --fixture` создаёт профиль с тестовым окном. Для запуска откройте файл или используйте `start` с новым именем файла. При наличии современного `wsb.exe` команда сохраняет ID гостевой среды рядом с профилем. `stop test.wsb` завершает записанную сессию, а при старом запускателе сессия завершается закрытием окна Sandbox. Профиль отключает сеть и перенаправление устройств, включает Protected Client и делится только копией тестового скрипта в папке для чтения.

`computer_plan(target="win32_desktop")` строит план для явно разрешённого фонового приложения. `computer_plan_history` учитывает результаты недавних вызовов этого клиента. Если по каждому варианту нет хотя бы пяти наблюдений, порядок остаётся детерминированным. `computer_sessions` показывает сессии браузера и Win32 Desktop в одном списке. Ссылки на окна, снимки UI Automation, ID кадров и квитанции визуальных операций доступны вызвавшему клиенту. При сравнении кадров `computer_verify` возвращает `changed_bounds` и группы изменений `changed_regions` в пикселях снимка с учётом указанного порога. При большом числе разрозненных областей отчёт возвращает общую рамку.

Для сравнения локальных vision-адаптеров запустите `python tools/benchmark_vision.py cases.json --adapter detector.py`. Разметка задаётся массивом JSON, например `[{"image":"dialog.jpg","query":"Save","boxes":[[10,20,80,30]]}]`. Для нескольких адаптеров повторите `--adapter`. Отчёт показывает попадания в первую и первые пять позиций, а также p50/p95 времени запроса.

`python tools/manage_extensions.py import PATH` импортирует каталог навыка или переносимого плагина, локальный checkout или ZIP. После проверки включите пакет в разделе «Расширения» или командой `python tools/manage_extensions.py enable skill NAME`. Подключённые агенты могут найти активные навыки через `skills_list` и прочитать их через `skill_read`. Каталог плагинов показывает обнаруженные MCP-компоненты и неподдерживаемые адаптеры. Для подключения stdio или Streamable HTTP добавьте в локальный `config.json` разрешение `plugin_mcp_grants` по примеру выше. Укажите SHA-256 пакета и разрешённые инструменты для каждого клиента. После перезапуска используйте `plugin_tools` и `plugin_call`. Повторный вызов с тем же `operation_id` возвращает известный результат без повторного запуска операции. При повторных сбоях соединения код `plugin_backoff` сообщает задержку для конкретного клиента. После неё следующий запрос открывает новое соединение.

### Подтверждение запуска команд

В `run_command` и `run_process` агент может передать `risk_explanation`. Пояснение на языке пользователя должно занимать до 600 символов и описывать цель команды, затронутые данные и возможные последствия. Пароли и токены указывать не нужно. Окно подтверждения показывает пояснение агента отдельно от причины, по которой PCBridge запросил подтверждение. Настроенные проверки действуют независимо от пояснения. Enter, Esc и тайм-аут отменяют запуск.

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
