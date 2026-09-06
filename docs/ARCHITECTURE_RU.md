# Архитектура Soft Hub 0.6.22

Документ фиксирует текущую архитектуру и реальные границы доверия Soft Hub 0.6.22. Пункты «нужно добавить» не являются обещанием уже существующей функции.

## 1. Продуктовая модель

Soft Hub — локальное single-user desktop-приложение для Python-автоматизаций. Релиз 0.6.22 имеет два автономных target: arm64 DMG для macOS и x64 installer для Windows 10/11. Пользователь устанавливает приложение и запускает его кликом; Python, Node.js, Git, Microsoft Visual C++ Redistributable и терминал не нужны, потому что каждый release bundle содержит подходящий managed CPython runtime и core Hub, а Windows bundle дополнительно включает нужные MSVC runtime DLL.

Свежий Hub поставляется без встроенного каталога и без предустановленных софтов: начальный список модулей пуст. Пользователь сам устанавливает доверенные `.softhub`/`.softhub.zip` локально либо получает новые версии из public GitHub Release напрямую или через Patch Radar.

Продуктовая модель:

- один каталог данных;
- одна центральная SQLite БД для профилей, модулей, запусков, событий и результатов;
- один Vault для связки EVM/Solana wallet, proxy/email/Twitter/AdsPower profile, реферальной топологии `child → parent` и отдельных глобальных Capsolver/AdsPower API keys;
- versioned plugins вместо копирования и ручного запуска папок;
- общая библиотека софтов и отдельные scoped workspaces `NFT`/`Тестнеты` поверх тех же modules/runs/results;
- отдельный subprocess на run;
- структурированный журнал и results вместо разрозненных console logs/CSV;
- явные read/testnet/mainnet действия и сохранение заявленного риска;
- пакетный запуск софтов параллельно либо строго сверху вниз;
- нижний Operations Shelf как пульт, а не вторая навигация: batch launch, все active runs и свежие ошибки; Vault/import/patch остаются в профильных разделах.

Это не облачный multi-tenant сервис, не контейнерный оркестратор и не security sandbox для недоверенного кода. Плагин — локальный привилегированный код, которому пользователь осознанно даёт выбранные секреты.

CLI и локальный браузерный режим остаются интерфейсами разработчика и тестирования, а не инструкцией по установке пользовательского приложения.

Термин «патч» в UI означает установку полного пакета новой версии. Hub не накладывает файловый diff на старую версию и не изменяет исходные папки пользователя.

## 2. Ключевые архитектурные решения

### 2.1. Core владеет общими данными, plugin — предметной логикой

Core владеет:

- идентичностью профиля;
- общей связкой EVM key / HTTP proxy / email / email password / Twitter / AdsPower profile, реферальной топологией и глобальными Capsolver/AdsPower API keys;
- шифрованием и unlock/lock;
- атомарным импортом профилей и ограждённым plaintext export;
- каталогом модулей и версий;
- безопасной проверкой manifest, необязательным catalog placement, legacy fallback и immutable snapshot разделов каждого run;
- metadata discovery публичных GitHub-патчей через Patch Radar;
- созданием одиночных и пакетных run, параллельной/последовательной очередью, глобальной конкуренцией subprocess и сохранённым per-run лимитом параллельных аккаунтов;
- журналом, progress и results;
- risk-классификацией и временными account leases на срок active run.

Plugin владеет:

- API/RPC клиентом проекта;
- всеми предметными действиями, их параметрами, обязательными ресурсами и форматом результата;
- проверками chain/contracts;
- retry и idempotency;
- интерпретацией ответа;
- безопасной остановкой, идемпотентностью и проверяемыми внешними ID операций;
- plugin-specific durable state, когда для него появится корректное стабильное размещение.

Core не содержит project-specific recipes, названий этапов, обязательных бизнес-параметров или правил конкретного API. Контракт плагина является адаптером к универсальным возможностям Hub, а не списком разрешённых проектов. Новый `SH-SOFTWARE-0.6/5` оставляет catalog и UI-подсказки полей необязательными там, где Hub может вывести безопасный понятный default; полная карточка `presentation` с изображениями остаётся обязательной. Package integrity, exact grants, закрытые options, process bounds, redaction и Vault least privilege не ослабляются. Строгий `/4` продолжает загружаться как legacy-контракт.

Catalog workspace не меняет эту границу владения. `general`, `nft` и `testnet` являются метаданными представления, а не capability: они не выдают secret, не меняют risk class, lease scope, batch policy или разрешённые сети. Общая библиотека показывает все модули; NFT и Тестнеты фильтруют карточки, метрики, запуски, результаты и Parsing-отчёты по core-derived `catalog_sections`. В `/5` catalog можно не указывать — fallback относит testnet-risk в `testnet`, остальное в `general`; если catalog указан, risk action всё равно проверяется отдельно и не выводится из раздела.

Плагин не должен напрямую обращаться к `hub.sqlite3`. Изменение таблиц core без миграции ломает безопасность, историю и совместимость данных.

### 2.2. Полная неизменяемая версия вместо in-place patch

Каждая пара `plugin id + SemVer` устанавливается в новый каталог. Это даёт изолированную `.venv` и основу для воспроизводимого runtime. Полная воспроизводимость дополнительно требует закреплённых dependency artifacts/hashes. Обновление разрешено только на более новую SemVer; downgrade и повторная активация прежней версии недоступны. Exact текущая версия с тем же archive SHA-256 считается уже установленной, а та же SemVer с другим содержимым отклоняется.

После первой GitHub-установки core связывает `owner/repository` с manifest `plugin id`, а конкретную версию — с release tag, asset name/URL и archive SHA-256. Эта identity не приходит от renderer. Один repository не может сменить plugin id, один id не может незаметно сменить repository, а прежняя SemVer не может получить другой payload.

Извлечённый каталог технически доступен на запись текущему пользователю и процессу плагина, поэтому «неизменяемый» — архитектурный инвариант, а не защита файловой системой. Ручное редактирование установленной версии запрещено процессом эксплуатации.

### 2.3. Subprocess + JSONL вместо импорта plugin-кода в сервер

Hub не импортирует предметный plugin-код в основной процесс. `runtime/bootstrap.py` запускается отдельным Python и только он импортирует entrypoint. Граница обмена:

- один JSON context через stdin;
- JSONL events через stdout;
- обычный stderr как предупреждения;
- exit code и terminal event как итог.

Так сбой или `sys.exit()` плагина не обязан уронить HTTP-сервер и Vault. Это изоляция жизненного цикла, но не OS security isolation.

### 2.4. Terminal-ошибка не становится блокировкой

Любой запуск, который не доказал успешное завершение, получает terminal status `failed`; явно остановленный оператором запуск — `cancelled`. Это одинаково для `read`, `external_write`, `testnet_write` и `mainnet_write`. Статус не утверждает, что внешнего side effect не было.

При terminal transition Hub в той же DB-транзакции снимает все leases и pins этого run. Ошибка, crash, restart или force stop поэтому не занимают аккаунты бессрочно и не требуют операторского подтверждения для rerun.

У этой простой модели есть осознанная цена: Hub не защищает от дублирования внешней мутации после неоднозначного сбоя. Автор plugin обязан использовать бизнес-ключи идемпотентности и durable public operation IDs. Перед повтором такого write оператору следует проверить explorer, API или внешний кабинет. Plugin может добавить отдельное read-only действие проверки, но Hub не требует и не запускает его автоматически.

Опциональный review/hide только убирает ошибку из живого уведомления. Он не переписывает error, account states, results и events и никогда не разрешает и не блокирует rerun.

### 2.5. Shared Vault вместо разрозненных хранилищ

Общие credentials импортируются один раз и выдаются плагину по declared secret kinds. Legacy `database.enc`, `vault.enc` и plaintext input не должны мигрировать внутрь пакета.

Отдельный предметный operational journal плагина не является копией общего Vault и не должен насильно помещаться в таблицу `accounts`: у него другой lifecycle и схема миграций.

### 2.6. Управляемый runtime внутри desktop-приложения

Release-сборка не зависит от Python, установленного у пользователя. Перед упаковкой `scripts/prepare_runtime.py` явно выбирает `darwin-arm64` либо `win32-x64` и:

- загружает закреплённый архив CPython 3.12.13 для целевой OS/архитектуры;
- проверяет заранее заданный SHA-256 архива;
- устанавливает зависимости core из `requirements-runtime.lock`; для Windows разрешены только бинарные `cp312-win_amd64` wheels, без локальной компиляции;
- копирует `soft_hub` в runtime и сохраняет закреплённый offline wheel `pip` для подготовки plugin `.venv`;
- записывает marker с идентификатором runtime, точным lock metadata и hash исходников, затем выполняет import/crypto self-check;
- для Windows проверяет PE AMD64 у `python.exe`, DLL и всех `.pyd`, а также наличие `python312.dll`, `vcruntime140.dll` и `vcruntime140_1.dll`.

Electron Builder помещает результат в `Resources/python`. Hook `beforePack` сопоставляет Electron target с runtime marker и блокирует смешивание macOS/Windows либо arm64/x64. В packaged-режиме launcher рассматривает только embedded interpreter, запускает его с `-I` и не использует `PATH`, системный Python или `SOFT_HUB_PYTHON` как fallback. Если managed runtime отсутствует или не проходит probe, приложение завершается с предложением переустановить его из соответствующего DMG/Windows installer.

Успех cross-build не заменяет release gate: core проходит smoke **из установленного desktop artifact**, а каждый распространяемый отдельно plugin — из реально установленного package на каждой заявленной OS/architecture. В частности, наличие Windows target само по себе не доказывает, что native plugin wheels совместимы с CPython 3.12 x64.

Запуск из исходников устроен отдельно: разработчик может выбрать Python через `SOFT_HUB_PYTHON` либо использовать локальный Python 3.12. Это dev/test surface и не часть пользовательского контракта.

## 3. Карта компонентов

```text
Soft Hub.app / Soft Hub.exe
├── Electron window
├── managed CPython 3.12 + core
└── schemas/docs
          │ HTTP 127.0.0.1 + X-Soft-Hub-Token
          ▼
    HubApplication / API
    ├── Database ───────────── external <data-dir>/hub.sqlite3
    ├── Vault ──────────────── AES-GCM account и global secrets
    ├── GitHubPatchFeed ────── public Patch Radar metadata
    ├── PluginManager ──────── inspect / install / prepare / remove
    └── RunManager
          │ spawn version .venv или managed Python; stdin context
          ▼
    runtime/bootstrap.py
          │ import package.module:function
          ▼
    plugin entrypoint
          │ SDK events → JSONL stdout
          └ print/traceback → stderr
```

Файловая ответственность:

| Компонент | Файл | Ответственность |
|---|---|---|
| Desktop shell | `electron/main.cjs` | Запуск Python core, sandboxed renderer, навигация только на origin Hub. |
| Release runtime builder | `scripts/prepare_runtime.py`, `requirements-runtime.lock` | Закреплённый CPython/dependencies, проверка SHA-256, сборка и self-check managed runtime. |
| Core process host | `soft_hub/__main__.py` | Аргументы, loopback server, URL с token, lock Vault при штатном завершении; CLI-вход используется разработчиками. |
| Instance ownership | `soft_hub/instance_lock.py` | Межпроцессный exclusive lock одного data directory. |
| HTTP boundary | `soft_hub/api.py` | Локальные маршруты, token/Host/Origin checks, ограничения body, static UI. |
| Paths/config | `soft_hub/config.py` | Версии, лимиты, data directory. |
| SQLite | `soft_hub/database.py`, `migrations/` | Миграции и короткие соединения/транзакции. |
| Credentials | `soft_hub/vault.py` | Нормализация, шифрование account/global secrets, импорт 1:1, ограждённый export и выборочная выдача. |
| Patch discovery | `soft_hub/github_patches.py` | Сканирование metadata публичных GitHub `.patch` repositories и строгий выбор release asset. |
| Package manager | `soft_hub/plugins.py` | Manifest/ZIP validation, checksums, forward-only version install, venv и удаление модуля. |
| Run host | `soft_hub/runner.py` | Очередь, leases, subprocess, protocol, redaction, run/account statuses и results. |
| Protocol adapter | `soft_hub/runtime/bootstrap.py` | Context decode, import/call, sync/async, signals, terminal events. |
| Author API | `soft_hub/sdk.py` | `HubContext`, `HubAccount`, bounded `map_accounts`, direct-parent helpers, protect-secret control-frame, telemetry/cancel. |

## 4. Каталог данных

Packaged desktop получает системный пользовательский путь через Electron `app.getPath('userData')`. Для release targets это по умолчанию:

```text
macOS:   ~/Library/Application Support/Soft Hub
Windows: %APPDATA%\Soft Hub
```

Каталог данных находится вне app bundle/install directory. Замена `/Applications/Soft Hub.app` или установленной Windows-версии обновляет Electron, core и managed runtime, но не удаляет Vault, профили, установленные плагины, журнал и результаты. Удаление только приложения также оставляет user-data на месте.

`SOFT_HUB_DATA_DIR` и `--data-dir` считаются dev/test override, а не пользовательским способом установки. При запуске из исходников путь выбирается так:

- `SOFT_HUB_DATA_DIR`, если задан;
- macOS: `~/Library/Application Support/Soft Hub`;
- Windows: `%APPDATA%\Soft Hub`;
- Linux: `${XDG_DATA_HOME:-~/.local/share}/soft-hub`;
- `--data-dir` имеет явный приоритет при создании `HubApplication`.

Технически desktop launcher тоже принимает `SOFT_HUB_DATA_DIR`, чтобы изолировать dev/smoke-запуск. Это неподдерживаемая для обычного пользователя настройка: production UX всегда исходит из системного user-data каталога.

Структура:

```text
<data-dir>/
├── .soft-hub.lock
├── hub.sqlite3
├── hub.sqlite3-wal              # может существовать во время работы
├── hub.sqlite3-shm              # может существовать во время работы
├── plugins/
│   ├── .staging/
│   └── <plugin-id>/
│       └── <version>/
│           ├── hub.plugin.json
│           ├── hub.checksums.json
│           ├── plugin/...
│           └── .venv/...        # только после prepare
├── imports/                     # временный upload, удаляется после install
├── runs/
│   └── <run-id>/scratch/
└── logs/
```

Root и создаваемые каталоги получают `0700`, SQLite и загружаемый архив — `0600`, насколько это поддерживает ОС. Извлечённые каталоги создаются с `0700`, файлы — `0600`.

Scratch уникален для run, но сейчас не очищается. Он не является постоянным API результатов и не является стабильным plugin storage. Загруженный исходный ZIP после установки удаляется; БД хранит только его SHA-256, а установленный каталог — распакованное содержимое.

## 5. Центральная SQLite БД

`Database` открывает отдельное соединение на операцию/поток и включает:

- foreign keys;
- WAL;
- `synchronous=NORMAL`;
- busy timeout 15 секунд;
- `BEGIN IMMEDIATE` для явно сгруппированных изменений.

Версионированные SQL migrations применяются по имени `NNN_*.sql` и фиксируются в `schema_migrations`.

### 5.1. Группы таблиц

| Область | Таблицы | Что хранится |
|---|---|---|
| Vault | `vault_meta`, `accounts`, `account_secrets`, `vault_secrets` | KDF/verifier, plaintext control metadata labels/address/fingerprints, encrypted account bundle и глобальные secrets. |
| Plugins | `modules`, `module_versions`, `github_module_sources` | Текущая версия, manifest, health, enabled, paths, immutable archive SHA и core-owned GitHub source/version identity. |
| Runs | `runs`, `run_batches`, `run_batch_items`, `run_events`, `results`, `run_account_states`, `run_account_pins` | Статусы, режим и порядок пачки, сохранённые `account_concurrency`, immutable `catalog_sections_json` и snapshot `output`, per-account identity/progress, target/direct-parent pins, redacted события и результаты. |
| Concurrency | `account_leases` | Пара `chain_id + account_id` для chain write; внутренний service-scope + account для `external_write` и exclusive referral-parent access. |
| Core | `settings`, `schema_migrations` | Настройки и применённая схема. |

`module_versions` — внутренняя identity-история: она сохраняет неизменяемое соответствие `id + SemVer + archive SHA` между обновлениями и после удаления модуля. Удаление в 0.6.22 стирает весь исполняемый каталог и `.venv`, помечает версии неактивными, но оставляет невидимые tombstones и GitHub source identity. Поэтому удалённая версия не может быть активирована снова, более старая не может быть установлена, а прежний `plugin id` нельзя незаметно перенести в другой repository. Уже утраченные прежними версиями Hub identity-записи автоматически не реконструируются. История выполненных запусков дополнительно опирается на собственные snapshots в `runs`, результаты и события. Таблица не является пользовательской функцией отката: актуальный runtime определяется только активной записью `modules`.

`run_events`, `results`, module manifests, labels, адреса, masked email/proxy labels, `twitter_configured` и fingerprints не шифруются Vault. Account/global secret payload шифруется. Plaintext в SQLite не означает публичность в UI/API: locked boundary скрывает и эту metadata. Поэтому plugin output обязан быть очищен до отправки, а backup всей БД всё равно считается чувствительным.

Миграция `008_run_account_concurrency.sql` добавляет к `runs` эффективный лимит `1..20`; `009_run_account_pins.sql` фиксирует роли `target` и `referral_parent` на срок run и не даёт удалить используемый аккаунт; `010_github_module_sources.sql` сохраняет связку `(module_id, version)` с repository/release/asset/archive identity и каскадно удаляет её вместе с версией; `011_result_statistics.sql` добавляет `runs.output_schema_json`, чтобы завершённый отчёт читал схему своего запуска, а не текущий manifest; `012_run_catalog_sections.sql` добавляет `runs.catalog_sections_json`; `013_account_revisions.sql` добавляет monotonic account revision с backfill `1` для CAS-редактирования и согласованного run admission; `014_solana_credentials.sql` добавляет только plaintext flag `solana_configured` и optional unique fingerprint, тогда как сам Solana keypair остаётся внутри AES-GCM payload; `015_batch_execution_mode.sql` добавляет к `run_batches` режим `parallel|sequential` и backfill `parallel` для прежних пачек. При upgrade catalog-миграция восстанавливает раздел из exact `module_versions`; для soft-removed записи допускает manifest из `modules` только при точном совпадении версии run. Если manifest отсутствует, повреждён или относится к другой версии, безопасный fallback — `general`. NFT никогда не угадывается по старому описанию, поэтому полностью удалённая до 0.6.15 NFT-история автоматически не реконструируется. Все новые run получают immutable catalog snapshot при создании, и он переживает последующее обновление или удаление модуля. Это разные механизмы: колонка concurrency ограничивает workers внутри subprocess, pins удерживают согласованную identity/topology, account revision закрывает preflight/admission race, `account_leases` предотвращает конфликтующие write-действия, GitHub source table не позволяет Patch Radar доверять renderer metadata или повторно предлагать exact установленный release, batch mode сохраняет порядок исполнения, а snapshots output/catalog сохраняют смысл и расположение исторических результатов.

Наличие `vault_meta` и успешный unlock не означают наличие account row. Vault — контейнер и состояние ключа, а импортированный профиль — отдельная запись `accounts` + `account_secrets`. Корректное начальное состояние после создания Vault — `vault.exists=true`, `vault.unlocked=true`, `accounts=0`; onboarding и run UI обязаны показывать импорт как отдельный незавершённый шаг.

### 5.2. Почему одна БД не означает одну схему для всего

`hub.sqlite3` — control-plane database. Добавлять туда таблицы каждого проекта напрямую нельзя:

- core migrations начнут зависеть от жизненного цикла плагина;
- смена plugin-кода без согласованной forward migration не сможет безопасно изменить state schema;
- удаление/отключение плагина затронет core;
- ошибочный SQL плагина получит доступ к Vault metadata и журналам.

Правильная будущая модель для stateful FSM-плагина: стабильный `<data-dir>/plugin-data/<plugin-id>/`, отдельная SQLite БД плагина, versioned migrations внутри плагина и backup policy Hub. Такого `plugin_data_dir` в текущем `HubContext` пока нет.

## 6. Vault

### 6.1. Создание и unlock

Мастер-пароль должен иметь минимум 14 символов и хотя бы 6 разных символов. При создании:

1. Генерируется 16-byte salt.
2. Ключ длиной 32 bytes выводится через scrypt: `N=32768`, `r=8`, `p=1`.
3. AES-GCM шифрует verifier с отдельным 12-byte nonce и AAD `vault-meta-v1`.
4. Derived key остаётся в `bytearray` памяти процесса Hub.

При unlock verifier проверяется authenticated decryption. Пароль не записывается. При lock bytearray заполняется нулями и ссылка удаляется.

Create/unlock не создаёт профиль и не генерирует расходники. Эти операции лишь делают доступным ключ шифрования; профиль появляется только после отдельного успешного импорта в `accounts`/`account_secrets`.

Ограничение: Python, криптобиблиотеки и JSON создают дополнительные immutable bytes/strings, которые нельзя гарантированно обнулить. Нет OS keychain, аппаратного enclave, idle auto-lock или recovery key. Процесс того же пользователя с достаточными debug-правами, root/administrator, malware или скомпрометированная ОС находятся вне модели защиты.

### 6.2. Граница заблокированного Vault

Когда Vault существует, но не разблокирован, bootstrap не раскрывает даже косвенную account/run metadata: `accounts`, `runs` и `results` возвращаются пустыми, счётчики accounts/results — нулевыми, а признаки настройки Capsolver/AdsPower — `null`, а не `false`. Безопасные агрегаты активных и требующих внимания операций могут оставаться видимыми, но не содержат labels, account IDs или результатов.

Прямые account/run/result projections, включая детали, account states, events и выгрузку технического журнала, возвращают HTTP `423 Locked`. Тот же gate применяется к `POST /api/modules/<id>/run`, `POST /api/runs/batch`, stop/force-stop и review/hide. Публичный API не допускает новый run с закрытым Vault; кроме того, любое внутреннее действие с выбранным account требует key даже при пустом наборе secret grants, потому что UUID, label и EVM address тоже считаются защищённой пользовательской metadata.

Renderer не является единственной защитой, но при lock дополнительно очищает accounts/runs/results, формы и открытые панели из памяти/DOM. Счётчик эпохи защищённых запросов делает ответы, отправленные до блокировки, устаревшими и не позволяет им повторно заполнить UI после lock. Desktop security lifecycle сначала синхронно скрывает окна и уничтожает renderer context, затем повторяет idempotent core lock до успеха и загружает свежий renderer с password gate; `resume`, `unlock-screen` и обнаруженный heartbeat gap повторно утверждают gate. Не отвечающий core точечно завершается и запускается заново в закрытом состоянии. Gate снимается только после свежей проверки `vault.unlocked=true`, поэтому stale UI после сна недоступен даже при задержке loopback API.

### 6.3. Импорт профилей

Основной UI-контракт 0.6.20 — таблица из прежних пяти колонок и необязательной шестой:

```text
private_key,proxy,email,twitter,adspower_profile,solana_private_key
```

Её можно вставить из буфера или загрузить как CSV/TSV/TXT. Разделитель определяется в порядке tab, `;`, `,`; принимаются оба canonical header — старый пятиколоночный и новый `private_key,proxy,email,twitter,adspower_profile,solana_private_key`. Первые три ячейки каждой строки обязательны; Twitter, AdsPower profile ID и Solana keypair могут быть пустыми. Пять колонок означают, что Solana-поле omitted и при re-import сохраняется; присутствующая пустая шестая ячейка означает explicit clear. Альтернативный ручной режим принимает отдельные списки EVM private keys, HTTP proxies и emails одинаковой ненулевой длины; Twitter, AdsPower profile ID и Solana keypairs необязательны, но заполненный список тоже должен совпадать по длине.

Email passwords и labels необязательны, но если переданы, их количество тоже должно совпадать. EVM private key нормализуется к `0x` + 64 lowercase hex, адрес получается через `eth-account`. Solana credential принимается только как полный 64-byte Ed25519 keypair в canonical base58 либо JSON-массиве Solana CLI; Hub проверяет byte bounds и соответствие public half seed, затем хранит canonical base58. 32-byte seed, mnemonic, address, hex и malformed keypair отклоняются. Proxy нормализуется к `host:port:user:password`; HTTPS proxy отвергается.

Для EVM key, optional Solana keypair, proxy и email вычисляются SHA-256 fingerprints. В одном импорте и между аккаунтами они уникальны. Повтор того же EVM key обновляет существующий профиль; Solana keypair/proxy/email другого профиля присвоить нельзя.

На каждый account создаётся один JSON secret bundle с EVM private key, optional Solana private keypair, полным proxy, email, optional email password, optional Twitter, optional AdsPower profile ID и nullable `referrer_account_id`. Он шифруется AES-GCM с новым nonce и AAD `account:<uuid>:v1`. При повторном импорте существующего EVM private key parent-связь и omitted optional fields сохраняются. В plaintext `accounts` остаются:

- UUID, label, EVM address;
- fingerprints;
- proxy endpoint без credentials;
- masked email;
- только признаки `email_password_configured`, `twitter_configured`, `adspower_configured` и `solana_configured`, но не сами значения;
- tags/status/timestamps.

Импорт атомарен: ошибка в одной строке не должна оставлять частично записанный batch.

`PATCH /api/accounts/{canonical-uuid}` принимает только `expected_revision` и непустой closed map `changes`. Для поля разрешена точная операция `{"op":"set","value":...}`; optional credentials/tags, включая Solana keypair, также поддерживают точный `{"op":"clear"}`. Renderer никогда не запрашивает текущий plaintext: он строит patch из заполненных replacement-полей и явных clear-команд. В одной `BEGIN IMMEDIATE` транзакции Vault сверяет revision, pins/leases, decrypt integrity и uniqueness, объединяет только заявленные поля, создаёт новый nonce/ciphertext и увеличивает revision. Account UUID остаётся стабильным даже при смене private key; address/fingerprints меняются для будущих run, а исторические run snapshots остаются прежними. Повторный импорт известного EVM key использует ту же busy-границу и сохраняет omitted password/label/tags/Solana keypair.

### 6.4. Глобальные secrets и plaintext export

Capsolver и AdsPower API keys хранятся отдельно от account bundle в `vault_secrets`, каждый шифруется AES-GCM со своим nonce/AAD и представлен в публичном API только статусом настройки. Оба ключа должны содержать минимум 4 символа и вводятся только в capability-карточках **Capsolver API** и **AdsPower Local API** раздела **Аккаунты**; в общих настройках и run options полей для них нет. AdsPower profile ID остаётся отдельным полем каждого account. Значение выдаётся run только при exact action grant `capsolver_api_key` либо `adspower_api_key`; SDK предоставляет их через `context.settings`.

Plaintext account export — осознанно опасный перенос данных, а не backup. Backend требует уже разблокированный Vault и повторную проверку мастер-пароля; отдельной подтверждающей фразы нет. Основной формат — минимальный XLSX без formulas/macros/external links, где шесть колонок `private_key,proxy,email,twitter,adspower_profile,solana_private_key` записаны как SpreadsheetML `inlineStr`; это сохраняет прежний порядок первых пяти колонок, добавляет Solana в конец и закрывает CSV formula injection при открытии в spreadsheet. Для совместимости endpoint без `format` по-прежнему возвращает lossless UTF-8 BOM raw CSV, а `format: "xlsx"` — безопасный для Excel вариант. Raw CSV предназначен только для автоматического round-trip и не должен открываться в Excel/Sheets. Email passwords, labels/tags, реферальные связи/коды и глобальные Capsolver/AdsPower API keys не экспортируются. Оба ответа имеют `Cache-Control: no-store`; после сохранения защита Vault к файлу больше не применяется.

### 6.5. Реферальная топология

Реферальная сеть хранит только зашифрованную топологию `child → direct parent`: в account payload есть nullable `referrer_account_id`. Project-specific referral/invite codes в Vault отсутствуют. UI показывает полный графический rooted forest: roots стоят сверху отдельных веток, descendants раскладываются по уровням, а направленные paths связывают каждый parent с direct children. Независимое состояние камеры хранит pan/zoom между локальными rerender, wheel сохраняет graph-point под курсором, fit-all допускает масштаб ниже обычного UI-порога для очень широкого леса, а мини-карта проецирует graph bounds и текущий viewport. В 0.6.15 контейнер карты вычисляет доступную высоту окна и больше не обрезает нижнюю часть canvas. Поиск, выбор узла, путь до root, node inspector и команды перестройки меняют только draft topology; viewport, координаты и layout не persist-ятся. Пользователь назначает одного direct parent или делает account root, но никогда не видит поля project code.

`POST /api/accounts/referral-topology` принимает полный CAS snapshot:

```json
{
  "expected_revision": "<64 lowercase hex>",
  "relationships": [
    {"child_account_id": "<canonical UUID>", "parent_account_id": null}
  ]
}
```

Каждый существующий account должен встретиться ровно один раз; максимум — 10 000 строк. В одной транзакции backend сверяет revision, canonical UUID, полное покрытие, неизвестных parents, self-links и циклы, затем перешифровывает payloads полного snapshot. Любая ошибка откатывает весь batch. Повторный импорт сохраняет parent link; удаление не занятого parent атомарно превращает его прямых детей в roots. Аккаунты, зафиксированные `run_account_pins`, удалить нельзя.

На первом успешном unlock миграция `vault_referral_topology_only_v1` атомарно вычищает legacy code-bearing поля из каждого payload, сохраняет только валидные parent links и ставит marker. Эти старые имена существуют только как предмет scrub-миграции, а не как разрешённый контракт.

Рекомендуемый контракт для новых пакетов — `SH-SOFTWARE-0.6/5` с `compatibility.hub >=0.6.22`; строгий `/4` и более ранние версии сохраняются для совместимости. Referral-aware action при необходимости объявляет `action.referral.mode: "project_runtime"`, `parent_required`, `parent_access` и отдельные exact parent permissions/resources. Runner строит согласованный plan только для выбранных targets и их уникальных direct parents, фиксирует topology/account revisions, создаёт pins ролей `target`/`referral_parent`, а при `parent_access: "exclusive"` получает service lease перед выполнением. Полного графа или ancestor closure subprocess не получает.

SDK разрешает получить parent только через `context.referrals.parent_for(child.id)` либо bounded `context.referrals.parents`; `context.referral_levels` даёт уровни только выбранных targets. Проектный код получает, кэширует и применяет сам плагин. Сразу после fetch, до любого log/result/exception/print, он вызывает `context.protect_secret(code)`: exact value проходит неперсистируемым control-frame в память host Redactor текущего run, но не становится context option/event/result/summary/log/file и не сохраняется в Hub. Raw output остаётся запрещённым.

### 6.6. Выдача плагину

Перед run Vault строит bundle:

```text
id, label, evm_address + только permissions.secrets
```

На уровне построения bundle account-free action при пустом `permissions.secrets` не требует Vault key, хотя публичные start/batch endpoints всё равно закрыты общим unlock gate. Если выбраны accounts, key обязателен даже при пустом наборе grants; bundle тогда содержит только `id`, `label` и `evm_address`. `solana_private_key` не является публичной identity: canonical base58 keypair добавляется только при exact permission/resource grant. При наличии grants Vault расшифровывает payload и добавляет только разрешённые поля. Это реальное least-data ограничение на уровне context. Затем JSON context записывается в stdin subprocess, после чего host очищает свои ссылки на временные account/context structures. В процессе плагина выданные секреты существуют в plaintext.

Для referral-aware `/4` и `/5` target bundles строятся по action grants и дополнительно содержат безопасные topology-поля `referrer_account_id` и относительный `referral_depth`; direct-parent bundles строятся отдельно и только по `action.referral.permissions/resources`. Parent, который не нужен ни одному выбранному target, не выдаётся. В context дополнительно передаются immutable links/levels и уже зажатый `account_concurrency`; project referral code там нет.

Vault не защищает от уже авторизованного plugin-кода: получив secret, тот может отправить его в сеть или записать на диск. Поэтому установка плагина — решение о доверии к коду и зависимостям.

## 7. Lifecycle пакета

### 7.1. Inspection

До распаковки `PluginManager.inspect_archive()` проверяет:

- размер архива, число entries и суммарный unpacked size;
- переносимость и безопасность каждого пути;
- отсутствие symlink/duplicates/casefold conflicts/zip-bomb признаков;
- наличие root `hub.plugin.json` и `hub.checksums.json`;
- manifest version/ID/runtime/permissions/actions;
- `SH-SOFTWARE-0.6/5` для новых пакетов: Hub `>=0.6.22`, exact action grants/resources и безопасные defaults для необязательных catalog/options/UI hints; строгий `/4` и более ранние контракты остаются совместимыми;
- точное совпадение списка checksum paths со всеми файлами;
- SHA-256 каждого файла через constant-time compare;
- наличие requirements path, если он объявлен.

Manifest и checksums ограничены 2 MB каждый. Вычисляется SHA-256 всего архива; при распаковке installer повторно считает каждый payload hash и перед активацией проверяет, что сам архив не изменился.

### 7.2. Install и активация

```text
upload → imports/<uuid>.softhub.zip
       → inspect
       → plugins/.staging/<uuid>/
       → повторная manifest validation
       → atomic os.replace в plugins/<id>/<version>/
       → transaction modules/module_versions
       → удаление upload
```

Новая версия сразу становится active, все прежние строки этого module получают `active=0`. Новая запись имеет `trust_status=local_unsigned`. Для существующего module сохраняется его enabled-флаг; trust не повышается. Core принимает только обновление на более новую SemVer; downgrade и активация прежней строки `module_versions` отсутствуют в UI, API и `PluginManager`.

При exception staging удаляется. Если target уже перемещён, но версия не зафиксирована в БД, installer пытается удалить и его.

### 7.3. Health и prepare

- Нет requirements или файл пуст/с комментариями → `ready`.
- Есть реальные dependency lines и нет host-owned marker → `needs_setup`.
- Prepare перед установкой снимает прежний ready-marker и переводит модуль в `needs_setup`, затем создаёт `.venv`, запускает pip с timeout 900 секунд и только после code 0 атомарно пишет новый marker с requirements SHA-256.

Install запрещает готовую `.venv`, не импортирует entrypoint и не делает smoke test. Готовность требует `.venv` Python и `.soft-hub-ready.json`, совпадающий с текущим requirements; отдельной проверки целостности всех установленных packages всё ещё нет.

Managed Python приложения и `.venv` плагина — разные слои. Первый доставляет и запускает core без системного Python. Второй создаётся командой **Подготовить** внутри конкретной установленной версии плагина и содержит её зависимости. В packaged app базовым interpreter для такой `.venv` служит managed runtime; обновление runtime меняет его fingerprint и требует пересоздания несовместимого plugin environment.

### 7.4. Patch Radar

Patch Radar принимает GitHub username или точный HTTPS URL `https://github.com/<owner>`, нормализует owner и сохраняет его в settings. Он читает первую страницу — не более 100 public repositories — и оставляет только имена с точным case-insensitive суффиксом `.patch`. Для каждого кандидата читается только latest release.

Карточка готова к установке лишь при ровно одном asset с case-insensitive суффиксом `.softhub` или `.softhub.zip`; обычный `.zip` не подходит. Metadata должна дополнительно пройти ранние границы: `asset.size` — целое число от 1 byte до 256 MB, `browser_download_url` — не длиннее 2048 символов и безопасный GitHub release URL того же owner/repository. Сканирование не скачивает и не устанавливает пакет. После явной команды оператора downloader отдельно применяет собственный лимит 256 MB, а затем передаёт файл в обычный inspection/install pipeline.

До первой установки repository имеет состояние `untracked`: candidate version из согласованных release tag/asset filename — лишь hint, окончательные `id/version` даёт инспекция manifest. Успешная установка создаёт core-owned binding в `github_module_sources`. После этого Radar сравнивает только доверенную repository identity и самую высокую SemVer из `modules + module_versions`, включая tombstones:

- exact candidate/active version → `installed`, install CTA отсутствует;
- candidate новее → `update_available`, установка разрешена;
- активная версия новее → `newer_installed`, downgrade блокируется;
- после удаления exact/older release → `removed_current`/`removed_newer_known`, install CTA отсутствует;
- после удаления только строго более новая версия → `removed_update_available`, установка разрешена из прежнего repository;
- metadata не даёт надёжной версии → `version_unknown`, установка блокируется;
- repository/id/asset противоречат сохранённой identity → `identity_conflict`, установка блокируется.

После download core повторно инспектирует package и запрещает downgrade. Exact текущая версия с тем же SHA-256 остаётся установленной без повторной активации; уже известная старая версия не может стать активной снова. Та же SemVer с другим архивом отклоняется как immutable-payload conflict. Поэтому переиздание asset под старым tag/version не считается обновлением; автор обязан выпустить новую SemVer.

Radar не отправляет GitHub token или Authorization, поэтому private repositories недоступны и действуют анонимные API rate limits. Обычная установка по GitHub URL также поддерживает только public releases; token-based доступ в 0.6.22 отсутствует.

### 7.5. Catalog projection

`PluginManager` валидирует raw catalog без изменения проверенного manifest. Допустимы `general`, `nft`, `testnet`; `general` взаимоисключающий, а `nft + testnet` — единственный overlap. Строгий `/4` дополнительно связывает testnet placement с risk. В свободном `/5` catalog необязателен и служит только размещению: Hub не запрещает автору показать один модуль в нужном workspace из-за состава его действий и не выводит risk из раздела.

Для legacy manifest helper вычисляет effective sections детерминированно: testnet risk становится `testnet`, всё остальное — `general`. NFT не выводится из copy, assets, network или chains. Active module projection получает отдельное `catalog_sections`, а raw `manifest.catalog` остаётся исходным.

При admission effective list сериализуется в `runs.catalog_sections_json` в той же транзакции, что run/account states/leases. Run, results overview и Parsing report читают этот snapshot, а не active module. Поэтому update, смена sections новой версией и uninstall не перемещают историю.

Renderer использует catalog как scope представления. **Софты** остаются полной библиотекой; **NFT** и **Тестнеты** показывают собственные hero/метрики, scoped cards, active/recent runs, results и reports. Один NFT-testnet модуль появляется в обоих scoped workspaces, но остаётся одной installed module identity; один run ID и result row не копируются.

## 8. Lifecycle запуска

### 8.1. Admission

До backend admission renderer строит run form из action schema. Для `account_mode: one_or_more` единственный существующий профиль выбирается автоматически; при нуле профилей вместо немого пустого списка показывается inline import CTA, а успешный импорт возвращает оператора к сохранённым module/action. При нескольких profiles выбор остаётся явным, доступен select-all, а смена action сбрасывает прежний multi-profile batch независимо от risk taxonomy. Если accounts существуют, но ни один не выбран, submit останавливается на клиенте с inline-ошибкой и переводом focus/scroll к account selector; `RunManager` независимо сохраняет серверную проверку минимума одного account.

Bounded numeric options рендерятся native range-контролами с отдельным точным number input без browser spinner. `multipleOf` имеет приоритет и для `integer`; без него integer использует шаг `1`, а обычный `number` получает безопасную UI-сетку без ослабления backend bounds. Ползунок применяется только к управляемой сетке до 1000 шагов, иначе renderer оставляет ручной ввод. `dual_range` объединяет два primitive keys в fieldset с двумя бегунками, но submission и snapshot сохраняют оба scalar values. Client проверяет `multipleOf` и `from <= to`; core независимо повторяет обе проверки до INSERT run. Reserved `account_concurrency` остаётся отдельным host-control с presets, числом выбранных профилей и подсказкой о волнах; он не смешивается с предметными параметрами action. Boolean `acknowledge_testnet_transactions` не рендерится среди options именно у `testnet_write`.

Одиночный и пакетный launch используют один schema renderer/collector. В batch renderer первоначально выбирает первый manifest action и позволяет открыть schema любого action независимо от resource-preflight; нехватка account/global resource влияет на admission, а не на доступность формы. Batch хранит draft options отдельно для каждого `module_id + module_version + action_id`, включая отдельный `account_concurrency`; смена action перестраивает и раскрывает только соответствующую карточку, а новая версия модуля не получает старый draft. Общего concurrency-state нет. На время submit все controls замораживаются, renderer фиксирует exact payload и idempotency key, а после неопределённого сетевого ответа повтор без правок использует ту же пару. Изменение любого значения после ответа сбрасывает key. Submit отправляет exact collected options каждого элемента, а не manifest defaults или состояние последней карточки.

Renderer не является trust boundary. Core независимо применяет закрытую action options schema: неизвестные keys, неверные JSON-типы, required/enum/range/multipleOf и malformed schema отклоняются до доступа к Vault и до INSERT run.

`RunManager.start()`:

1. Находит enabled/ready module.
2. Находит action в manifest.
3. Удаляет дубли account IDs с сохранением порядка.
4. Проверяет `account_mode`.
5. Для write-действий не запрашивает дополнительную фразу: явное нажатие запуска является единственным UX-сигналом.
6. После успешного testnet gate принудительно устанавливает `options.acknowledge_testnet_transactions=true`, если action schema объявляет это boolean-поле; значению клиента runner не доверяет.
7. Валидирует closed options и для `one_or_more` подставляет safe default, зажимает `account_concurrency` числом выбранных accounts и сохраняет effective `1..20`; строгий `/4` дополнительно ограничивает browser schema значением `5`, а `/5` оставляет меньший project-safe предел автору софта.
8. Проверяет exact target resources. Для `action.referral` строит snapshot выбранных links и уникальных direct parents, валидирует exact parent resources и `parent_required`.
9. Повторно валидирует, что активные версия/path/health модуля не изменились между preflight и admission.
10. Вычисляет effective catalog sections и сохраняет immutable `runs.catalog_sections_json`.
11. Снимает revision snapshot targets и referral parents, а внутри admission-транзакции повторно сравнивает его с БД.
12. Создаёт run со статусом `queued`, `run_account_states` и target/referral-parent pins в одной транзакции.
13. Запускает daemon thread выполнения; он получает глобальный subprocess slot, атомарно запрашивает необходимые write/exclusive-parent leases и только после этого расшифровывает bundles перед spawn.

Пакетный endpoint принимает UUID idempotency key, `execution_mode: parallel|sequential` и canonical request hash. Повтор того же payload возвращает прежние runs, а reuse ключа для другого payload или режима отклоняется. `parallel` остаётся default и сохраняет совместимость со старыми клиентами. Mainnet actions допускаются в обоих режимах, но сохраняют свой declared risk, exact grants, Vault/preflight и account/scope leases. Все элементы preflight-проверяются, revisions повторно сравниваются, а batch/runs/states/pins создаются в одной DB-транзакции, поэтому admission действует по принципу «всё или ничего». Leases намеренно не резервируются на admission.

В `parallel` каждый run сразу конкурирует за глобальный subprocess slot и нужные leases. В `sequential` ordinal из `run_batch_items` образует строгий барьер: run может перейти к slot/leases/Vault только когда terminal стали **все** более ранние ordinals, а не только непосредственный предшественник. Поэтому отмена будущего второго пункта не позволяет третьему обогнать ещё работающий первый. `failed` или `cancelled` предыдущего пункта считаются terminal и не прерывают оставшуюся очередь. Ожидающий пункт имеет stage `waiting_for_software`, не занимает process slot или account lease и не получает расшифрованный payload. После рестарта незапущенные sequential waiters безопасно завершаются как cancelled: runtime options/context для автоматического resume не persist-ятся.

Глобальный semaphore по умолчанию пропускает четыре **software subprocess** одновременно (`--max-concurrent` меняет предел). Это batch software concurrency. Отдельный сохранённый `runs.account_concurrency` ограничивает workers аккаунтов **внутри одного** subprocess; SDK передаёт его как `context.account_concurrency`, а `context.map_accounts()` создаёт не больше этого числа threads. Эти лимиты не заменяют друг друга.

Ожидание sequential turn, глобального slot и account lease прерываемое: worker проверяет cancellation через короткий timed loop. При lease-конфликте он сразу возвращает глобальный slot, помечает accounts как `waiting_for_account` и повторяет попытку, поэтому занятый аккаунт не блокирует запуск несвязанного софта. Stop у queued run завершается до расшифровки secrets, создания scratch и spawn; leases/pins освобождаются. Semaphore освобождает только worker, который действительно получил slot. Hub не контролирует самодельные plugin threads/async tasks, поэтому любой контракт требует соблюдать effective host limit, finite timeout, cancellation и join/cleanup внутри entrypoint.

### 8.2. Account leases

Непосредственно перед выполнением run для каждого выбранного account и каждого `permissions.chains` chain-write создаётся строка с expiry 30 минут. `external_write` создаёт одну строку на account во внутреннем service-scope, не заставляя автора указывать фиктивную chain. Referral parent с `parent_access: "exclusive"` получает отдельный service-scope lease; `shared_read` — только pin без lease. Все lease-группы одного run выдаются атомарно. Конфликт одинакового scope/account переводит второй run в ожидание; после освобождения он стартует автоматически. Read и одинаковый account в разных chain scopes могут работать параллельно. Event loop проверяет monotonic clock независимо от наличия вывода и продлевает lease раз в минуту.

Это защита от случайного параллельного write внутри одного экземпляра Hub, а не распределённый nonce manager:

- read-actions leases не берут;
- валидатор требует непустой `chains` только для `testnet_write`/`mainnet_write`, но ложную фактическую chain он обнаружить не может;
- второй Hub с тем же data directory блокируется exclusive lock, но отдельный legacy CLI leases не видит;
- фактическую chain плагина Hub не проверяет;
- при любом terminal outcome — success, failure, cancel, force stop или recovery после restart — все leases и pins run удаляются атомарно с финальным статусом;
- Hub не создаёт бессрочную safety hold и не требует ручного подтверждения для повтора;
- TTL и heartbeat не заменяют durable transaction journal.

При startup и при финализации Hub нормализует исторический `needs_attention` в `failed`, сохраняет журнал/результаты как evidence и освобождает оставшиеся leases/pins. Новый code path этот статус не создаёт.

### 8.3. Spawn

Для run создаётся новый `runs/<id>/scratch` с mode `0700`. Python выбирается из активной version `.venv`, иначе используется interpreter Hub. В packaged desktop это interpreter из `Soft Hub.app/Contents/Resources/python`; системный Python fallback отсутствует. В source/dev-режиме interpreter Hub задаёт окружение разработчика. Команда запускает core bootstrap и передаёт plugin root/entrypoint аргументами.

Subprocess получает:

- cwd = scratch;
- `shell=False`;
- новый process group/session;
- небольшой allow-list environment: системные PATH/temp/locale/certificate variables плюс Python hardening flags;
- stdin/stdout/stderr pipes UTF-8.

Из environment удаляются произвольные пользовательские variables, но это hygiene, не sandbox. Plugin может обращаться к filesystem и network с правами OS-пользователя.

Context сериализуется одной JSON-строкой и сразу закрывается stdin. Bootstrap ограничивает её 8 MiB. Помимо exact target/settings bundles он содержит сохранённый `account_concurrency`; referral-aware action получает отдельные exact parent bundles, bounded direct links и уровни выбранных targets, но не project referral codes. После decode bootstrap добавляет plugin root в `sys.path`, импортирует entrypoint и создаёт `started` event.

### 8.4. Protocol и сохранение

Bootstrap добавляет к каждому SDK event:

```json
{"protocol":"soft-hub-jsonl/1","seq":1,"type":"log","level":"info","message":"Этап завершён","data":{}}
```

Runner принимает только известные event types; account-scoped frame разрешён лишь для ID, сохранённого при admission текущего run. Stderr-строка становится warning event. После redaction:

- любой event попадает в `run_events`;
- для action без аккаунтов run-level `progress` монотонно обновляет `runs.progress`; значение вне `0..1` или регресс отклоняется;
- при наличии account projection run-level progress остаётся telemetry и не меняет итог: `runs.progress` монотонно получает `AVG(run_account_states.progress)` по всем выбранным аккаунтам, включая `queued=0`;
- `result` дополнительно создаёт строку `results`.
- `account_state` атомарно обновляет `run_account_states` со status/stage/progress/last_message.

Служебный `protect_secret` — отдельный control-frame, а не event. Runner принимает единственное exact string value длиной `4..4096`, ограничивает число/суммарный объём таких регистраций, немедленно добавляет value в in-memory Redactor и делает `continue`: frame не попадает в `run_events`, results или summary. Exact project code, следовательно, кратко присутствует в памяти plugin/host текущего run, но не persist-ится. Плагин обязан отправить frame сразу после fetch и до любого потенциального вывода; raw print/log всё равно запрещён.

`run_account_states` создаётся для всех выбранных ID в той же admission-транзакции, что и run/leases, и хранит snapshot label независимо от дальнейшего удаления аккаунта. Terminal account status может задать только `account_state`; обычный log/result обновляет activity, но не определяет успех. Read-only projection доступен через `GET /api/runs/{id}/accounts`. Operations Shelf параллельно читает bounded lanes `GET /api/run-accounts?scope=active` и `?scope=attention`: SQL-фильтр применяется до `LIMIT`, поэтому свежая история не вытесняет зависшую операцию, а response явно сообщает `truncated`.

Core не вычисляет проценты из времени, текста log или количества строк. Adapter задаёт фактические weighted milestones; runner проверяет диапазон и монотонность. `succeeded` account принудительно получает `1.0`; terminal `partial/failed/skipped/blocked/cancelled` не повышает процент и сохраняет последнюю подтверждённую точку. Поэтому индикатор отвечает на вопрос «какая доля объявленной работы подтверждена», а lifecycle status отдельно отвечает на вопрос «чем она закончилась».

Compatibility-исключение ограничено точными first-party Checkpoint/Sekai/Umia `1.0.0` и заранее перечисленными actions. Если такой старый adapter не отправил terminal `account_state`, core принимает только ровно один account-scoped result `kind=account_summary` с allowlisted status; duplicate, third-party ID/version, произвольный log и историческая проекция не подходят. Миграция `007` применяет тот же allowlist к уже завершённым `unknown/unreported` строкам. Новые плагины на этот bridge рассчитывать не могут.

Bootstrap не вычисляет counters по короткому списку истории: active runs и свежие terminal errors считаются SQL-агрегацией по всей таблице. В payload входят active runs с active-first cap 500 и последние 30 terminal runs; `runs_truncated` явно сообщает о переполнении operational lane. Поэтому старый живой процесс не исчезает из dock/poll после множества свежих завершений.

После трёх malformed stdout frames процесс принудительно завершается с protocol error. Одна строка ограничена 64 KB, весь run — 50 000 строками; превышение останавливает процесс. Bootstrap перенаправляет runtime `print()` в stderr, но import-time stdout остаётся опасным.

### 8.4.1. Parsing и safe report projection

Parsing не добавляет новый protocol event. Софт запускает обычный `read` action с `account_mode: one_or_more` и отправляет ровно один account-scoped `context.result()` с объявленным `primary_kind` на каждый начатый кошелёк. Манифест может добавить к action только декларативное представление:

```json
{
  "output": {
    "mode": "account_table",
    "title": "Статистика аккаунтов",
    "primary_kind": "account_snapshot",
    "columns": [
      {"key": "points", "title": "Очки", "type": "integer", "aggregate": "sum"}
    ]
  }
}
```

Граница умышленно узкая: не более 12 прямых scalar-колонок типа `string|integer|number|decimal_string|boolean`, до четырёх `sum|avg|min|max` на numeric-колонках. JSONPath, dotted keys, вложенные data, plugin templates и произвольный renderer отсутствуют. Это не расширяет permissions и не позволяет выдать мутирующий action за read-only parser.

При admission Hub фиксирует `output` в snapshot run вместе с account label/address. Таблица истории поэтому не зависит от текущего manifest после update или uninstall. Проекция якорится на `run_account_states` и присоединяет один `results` row точного `primary_kind`. Поэтому каждый selected account остаётся видимым даже при ранней ошибке, а итоговый status и системные counters берутся из authoritative lifecycle, не из result title/status.

`GET /api/results/overview` выбирает отчёт по run/module/action, а `GET /api/results/report?run_id=...` возвращает bounded safe report projection (до 2 000 строк): snapshot label/address, authoritative lifecycle и только объявленные scalar-поля результата. Renderer применяет к уже полученной полной проекции текущие search/filter, показывает объявленные агрегаты и из отфильтрованных строк формирует formula-safe CSV. Если response имеет `truncated: true`, renderer блокирует CSV целиком: неполная выборка не превращается в вводящий в заблуждение export. Отдельного export endpoint и XLSX для статистики нет. CSV не читает raw results и не добавляет undeclared payload fields. Строковые ячейки с первым `=`, `+`, `-`, `@`, tab, CR или LF получают ведущий апостроф; schema-typed `integer`, `number` и `decimal_string` сохраняются числами без апострофа, включая отрицательные значения.

Получение report projection требует открытого Vault; при lock backend отвечает `423`, а renderer очищает защищённое состояние и игнорирует запоздавшие ответы прежней эпохи. Скачивание доступно только пока Vault открыт, но уже сохранённый CSV является деклассифицированным plaintext-артефактом и больше не получает защиту Hub.

Старый action без `output` и исторический run без snapshot не проходят автоинференс произвольного `data`: UI сохраняет для них обычное групповое представление. Это fail-closed совместимости: старые патчи не ломаются, но их необъявленные payloads не превращаются в колонки автоматически.

### 8.5. Terminal semantics

| Условие | Итоговый status |
|---|---|
| `completed` и exit code 0 без account projection | `succeeded`, progress 1.0. |
| `completed` и exit code 0 с аккаунтами | `succeeded`, progress остаётся `AVG(account.progress)`; нормальный exit не маскирует раннюю ошибку косметическими 100%. |
| `cancelled` или exit code 130 | `cancelled` |
| Любая другая ошибка, crash или неоднозначный итог независимо от risk | `failed` |

Summary берётся только из `completed.data.summary`; отдельные results существуют независимо.

`POST /api/runs/<id>/review` — опциональное «скрыть ошибку» для terminal run-level `failed` либо run с account-level `partial`/`failed`/`blocked`/неисторическим `unknown`. Переход в `reviewed` идемпотентен, убирает run только из текущей error projection и сохраняет original error, events, results и account states. Он не является сверкой, не подтверждает внешний outcome и не влияет на возможность rerun: повтор доступен уже после terminal transition.

Если Hub стартует и видит старые `queued`, `starting`, `running` или `cancelling`, он помечает их `failed`, фиксирует restart в audit trail и атомарно снимает все leases/pins. Межпроцессный lock не даёт второму Hub выполнить recovery, пока первый владеет тем же data directory. Внешний outcome при этом остаётся недоказанным: перед повтором write его проверяют по durable business key/public operation ID.

### 8.6. Stop

Мягкий Stop API доступен только при `runtime.safe_stop === true`: Hub атомарно меняет status на `cancelling`, создаёт cancellation marker и сразу пишет host-event. Bootstrap следит за marker одинаково на macOS и Windows; на POSIX Hub дополнительно посылает process-group `SIGTERM`. Если bounded cleanup не уложился в grace period, остановка автоматически усиливается. Отдельный force-stop запускается одним нажатием, не зависит от manifest и завершает весь POSIX process group через `SIGKILL` либо Windows Job Object с `KILL_ON_JOB_CLOSE`; `taskkill /T /F` остаётся только fallback. Terminal status и снятие leases публикуются после завершения containment tree; гонка Stop с обычным finish не может вернуть terminal run в вечный `cancelling`.

На обеих платформах bootstrap превращает marker/сигнал в cancellation event, а plugin SDK отдаёт его плагину через `context.check_cancelled()`. Корректное мягкое завершение всё равно зависит от регулярного polling и bounded cleanup в самом плагине. Прерывание между подписью, broadcast и journal commit может оставить неопределённое внешнее состояние; один флаг manifest этого не исправляет.

При shutdown Hub перестаёт принимать runs, отменяет queued, сигналит всем активным процессам, ждёт ограниченный grace period и затем принудительно завершает оставшиеся. Electron при обычном выходе сначала вызывает cooperative shutdown API и удерживает закрытие desktop-процесса до завершения core. На POSIX runner владеет descendants через PGID, на Windows — через Job Object; terminal status и leases/pins финализируются только после очистки containment. Плагину запрещены detached children: platform-safe host stop не превращает произвольно отделившийся процесс или внешний side effect в доказанно отменённый. Поэтому перед ручным rerun write нужно проверить внешний outcome.

## 9. HTTP и desktop boundary

Core слушает только `127.0.0.1` на выбранном порту. API требует случайный `X-Soft-Hub-Token`; UI получает token из URL fragment. Дополнительно:

- Host должен быть `127.0.0.1` или `localhost` и не может подменить порт сервера;
- mutating request с Origin разрешён только с тем же hostname и портом, что у текущего UI;
- JSON body ограничен 2 MB;
- upload ограничен 256 MB;
- security headers запрещают framing/object/external scripts;
- сервер не пишет request path/body в обычный terminal log.

Отсутствующий Origin принимается, поэтому token остаётся главным API credential. Любой local process, получивший token, может обращаться к API. Нет TLS, remote auth, users/roles и сетевого режима — server предназначен только для loopback.

Electron включает renderer sandbox, context isolation, отключает Node integration и внешнюю навигацию. Единственное узкое исключение — явный CTA Patch Radar может передать системному браузеру уже проверенный HTTPS URL вида `github.com/<owner>/<repo>`; само Electron-окно на внешний origin не переходит. Эта sandbox относится к UI renderer. Она не помещает Python plugin subprocess в sandbox.

Desktop updater закреплён за `spr1ntray/soft-hub` и принимает только более новую stable SemVer из immutable GitHub Release. Один legacy bridge для `v0.6.14` допускается по встроенному exact pin release/commit/asset identity и SHA-256; любое отличие блокируется, поэтому это не универсальный обход immutability. Exact platform asset и `SHA256SUMS` должны быть единственными, иметь GitHub-computed SHA-256 digest и пройти независимые size/hash checks; cached installer повторно хешируется после Vault lock прямо перед `shell.openPath`. На updater commit-boundary оболочка уничтожает renderer до блокировки Vault; recoverable отказ показывает только свежий password gate. Renderer получает лишь очищенное состояние updater и не управляет URL или локальным путём. Metadata проверяется при старте и затем раз в шесть часов активной долгой сессии, но download/install требуют отдельных действий пользователя. Это защищает транспорт и уже опубликованный immutable либо exact-pinned release, а не owner account: независимой offline-подписи manifest пока нет.

Локальный preview для macOS собирается с ad-hoc подписью и не проходит Apple notarization; Windows installer также не имеет Authenticode-подписи. Оба artifact пригодны для локального тестирования и доверенной передачи, но не подтверждают издателя и не являются готовой схемой публичной доставки. Публичный macOS-релиз требует Developer ID Application, hardened runtime, notarization и stapling; Windows-релиз — доверенный code-signing certificate и проверку SmartScreen/reputation. Успешная cross-сборка сама по себе эти gates не проходит.

## 10. Честные границы доверия

| Граница | Что реализовано | Чего нет |
|---|---|---|
| Desktop runtime | Закреплённые `darwin-arm64`/`win32-x64` CPython archives, lock/source hash, `-I`, target marker; Windows PE AMD64/MSVC DLL/native-wheel checks. | Криптографически воспроизводимая сборка всех artifacts; runtime не изолирует плагины от ОС. |
| Desktop distribution | Автономные arm64 DMG и Windows 10/11 x64 installer; target/runtime mismatch блокируется; updater требует immutable release (кроме единственного exact-pinned `v0.6.14`), GitHub digests, SHA256SUMS и повторный hash перед OS launch. | Offline-подпись release manifest, Developer ID/notarization для macOS, Authenticode/SmartScreen reputation для Windows и installed clean-machine smoke обеих платформ. |
| Архив | Safe paths, лимиты, SHA-256 всех файлов и всего архива. | Подписи издателя, certificate chain, transparency log, доверенный registry. |
| Patch Radar | Ограниченное чтение metadata public `.patch` repositories, строгий latest asset, source/version binding, SemVer states и отдельный download limit. | Private repositories, GitHub token и доказательство доверия к найденному автору/asset. |
| Идентичность плагина | `id/version`, repository binding и immutable archive hash; exact/newer/unknown/conflict/downgrade различаются fail-closed. | Доказательство автора. Любой может пересобрать checksums. |
| Catalog workspaces | `/5` допускает необязательное presentation placement, `/4` сохраняет strict cross-check, для всех версий есть fallback и immutable run snapshot; renderer фильтрует общую библиотеку/историю. | Catalog не доказывает NFT, testnet, фактический risk/chain и не выдаёт permission. |
| Secrets в context | Exact grants отдельно для targets/settings и direct referral parents; код проекта отсутствует во входном context/options. | Защита секрета после выдачи plugin-коду. |
| Runtime referral code | `protect_secret` передаёт exact value неперсистируемым control-frame и регистрирует его в host Redactor. | Устранение краткого plaintext residency в plugin/host memory или разрешение логировать raw code. |
| Environment/cwd | Узкий env, отдельный scratch, subprocess. | OS/container sandbox, filesystem ACL profile, syscall restrictions. |
| Network | Плагин декларирует domains. | Firewall/DNS/proxy enforcement; allow-list пока не исполняется. |
| Chains | Declared chain IDs участвуют в leases. | Проверка RPC chain, contracts, calldata, суммы или nonce. |
| Browser/local service | Manifest валидирует AdsPower grants, `browser=true`, canonical local service и action resources; Vault хранит profile ID/API key, runner делает preflight. | Provisioning AdsPower/Chrome/driver, принудительный permission broker, endpoint/auth mediation, browser sandbox. |
| Output | Exact-secret и regex redaction, bounds/truncation. | Гарантия против encoding/fragmentation/custom token или записи в файл/сеть. |
| Dependencies | Отдельная `.venv`, captured pip output, timeout. | Подпись wheel, lock enforcement, запрет build hooks, malware scan. |
| Stop/restart | Process group signal, grace period, terminal `cancelled`/`failed` и освобождение leases/pins. | Транзакционная отмена внешнего side effect, автоматический resume или внешняя проверка. |
| Local UI | Loopback, random token, Host/Origin/CSP, Electron renderer sandbox. | Защита от malware того же OS-пользователя и remote multi-user deployment. |

Checksums отвечают на вопрос «файлы совпали с таблицей внутри этого архива?», но не «кто создал архив?». `permissions.network` отвечает на вопрос «что автор заявил?», но не «куда процесс способен подключиться?». Subprocess отвечает на вопрос «упадёт ли plugin прямо внутри core?», но не «может ли plugin прочитать доступные пользователю файлы?».

До появления signature verification и OS sandbox устанавливать следует только собственные или полностью проверенные плагины. Особенно опасен этап prepare: `pip` и build backend зависимостей выполняют код с теми же пользовательскими правами ещё до первого run.

## 11. Redaction и чувствительные данные

`Redactor` знает точные секреты текущего context и regex для EVM key, JWT и proxy. Он рекурсивно очищает event data, terminal summary и host error. Это снижает риск случайного логирования, но не является DLP.

Project-runtime referral code отсутствует во входном context и persistence Hub. После того как плагин сам получил его у проекта, `protect_secret` передаёт exact value в host memory неперсистируемым control-frame; только после этой регистрации host может вычистить совпадение из последующего stderr/events. Это временное residency, а не хранение и не автоматическая защита вывода, случившегося до frame.

События и results после redaction хранятся открыто в SQLite. Следовательно:

- plugin не отправляет email password, key, proxy credentials, cookie, access token, raw transaction;
- error payload содержит тип/код, а не полный request/response с headers;
- account связывается через UUID, не через secret;
- публичный transaction hash допустим, если продуктово нужен и не раскрывает нежелательную связь;
- backup БД и scratch всё равно чувствительны.

## 12. Core gaps до следующего уровня

### Release gate — до публичного распространения

- Подписать `.app` сертификатом Developer ID Application с подходящими entitlements и hardened runtime.
- Отправить сборку на Apple notarization, проверить результат и выполнить stapling для распространяемого DMG/app.
- Подписать Windows installer доверенным Authenticode-сертификатом и проверить его поведение под SmartScreen.
- Публиковать SHA-256 и выполнить installed-package smoke на чистом arm64 Mac под действующим Gatekeeper и чистой Windows 10/11 x64.
- Для каждого распространяемого отдельно plugin-пакета повторить установленный smoke на каждой OS/architecture из его `compatibility.os`; проверить, что Windows dependencies разрешаются готовыми `cp312-win_amd64` wheels без system Python/Node/VC++/compiler.

Текущие ad-hoc signed/not notarized macOS preview и unsigned Windows installer предназначены для локального тестирования и доверенной передачи, а не для публичного production-релиза.

### P0 — до stateful/mainnet плагинов

- Стабильный `plugin_data_dir` с quota, permissions, backup и versioned migration policy.
- Durable operation journal, бизнес-ключи и public operation IDs для финансовых writes.
- Installed clean-machine smoke Windows Job Object на реальных browser/AdsPower descendants и аварийном завершении desktop.
- Явный mainnet signer secret и lifecycle revoke/rotate.
- Проверка реального chain/domain внутри адаптеров; позднее — host policy.

### P1 — до browser и сторонних пакетов

- Интеграция уже существующих Twitter/Capsolver/AdsPower resources в новые версии нужных адаптеров; расширение schema остаётся нужным для Telegram/cookies и отдельного mainnet signer.
- Capability broker для browser/local services.
- Lifecycle hooks или заранее собранные runtimes для Playwright/Node, с lock/hash policy.
- Publisher signatures и allow-list доверенных ключей.
- OS-level sandbox/network egress policy на поддерживаемых платформах.

### P2 — эксплуатация

- Планировщик с timezone, jitter и лимитами, но только после idempotency/recovery.
- Зашифрованный backup/restore/rekey с проверкой целостности; реализованный plaintext account XLSX/raw CSV export его не заменяет.
- Очистка scratch и retention для events/results.
- Health check, который импортирует entrypoint в подготовленном runtime до активации.
- Compatibility enforcement по OS/Python.
- Retention и безопасная очистка неактивных version artifacts без потери identity/audit metadata.

Эти gaps нельзя «решить манифестом»: поле `network`, `state_model` или `heartbeat_seconds` не превращает декларацию в enforcement.

## 13. Backup и восстановление

В UI 0.6.20 нет зашифрованного backup/restore. Ограждённый plaintext XLSX/raw CSV export переносит только `private_key,proxy,email,twitter,adspower_profile,solana_private_key`, не сохраняет реферальную топологию, историю, плагины и глобальные Capsolver/AdsPower API keys и не является backup. Для согласованной ручной копии безопаснее:

1. Остановить новые runs и дождаться terminal-завершения активных запусков.
2. Заблокировать Vault.
3. Полностью завершить Hub, чтобы WAL был checkpointed и ключ исчез из процесса.
4. Скопировать весь data directory, а не только `hub.sqlite3`: нужны plugin versions/venv и run artifacts; при live-copy также потребовались бы `-wal/-shm`.
5. Хранить backup зашифрованным и проверить restore на отдельном data directory.

Замена или удаление `/Applications/Soft Hub.app` либо Windows application directory не затрагивает системный user-data каталог. Обратное тоже важно: копия одного app/installer не является backup пользовательских профилей, Vault, плагинов и истории.

Потеря мастер-пароля сейчас невосстановима. Backup SQLite не отменяет внешние транзакции и не доказывает состояние chain; для write-run нужны transaction hashes, durable business keys и operation journal.

`.venv` обычно лучше воспроизводить из закреплённых requirements, чем считать переносимым backup между ОС/архитектурами. Но исходные `.softhub.zip` Hub после install не сохраняет, поэтому release artifacts следует хранить отдельно вместе с их SHA-256.

## 14. Инварианты для code review

Любое изменение core или новый plugin должно сохранять следующие правила:

1. Secret не появляется в manifest, options, event, result, summary, path или exception text.
2. Plugin получает не больше secret kinds, чем объявлено.
3. Один `id/version` соответствует одному проверенному содержимому.
4. Read action не отправляет транзакции, ордера и финансовые подписи.
5. Write action перечисляет все chains и требует соответствующее подтверждение.
6. Неоднозначный write заканчивается `failed` или `cancelled`, но никогда ложным success; журнал/результат прямо говорит, что внешний outcome нужно проверить.
7. Retry после возможного side effect начинается с чтения внешнего state по бизнес-ключу/public operation ID; Hub не заменяет эту идемпотентность.
8. Safe stop означает проверенный recovery boundary, а не только обработчик SIGTERM.
9. Core DB меняется только migrations core; plugin state изолирован.
10. Legacy source directory не является runtime dependency установленного плагина.
11. Prepare/run не полагаются на cwd, user site packages или секретные env variables.
12. Checksums не называются подписью, subprocess не называется sandbox, declarations не называются enforcement.
13. Packaged app запускает core только из собственного managed runtime; системный Python не является скрытой runtime dependency пользователя.
14. Новый plugin рекомендуется выпускать по свободному `SH-SOFTWARE-0.6/5` с Hub `>=0.6.22`; строгий `/4` и более ранние контракты остаются load-compatible.
15. `account_concurrency` ограничивает per-run workers и не подменяется глобальным batch/subprocess semaphore; workers thread-safe, cancellable и bounded.
16. Referral topology содержит только `child → parent`; project code получает сам plugin, немедленно регистрирует через non-persisted `protect_secret` frame и нигде не сохраняет/не выводит.
17. Review/hide terminal/account-level ошибки только скрывает notification и сохраняет evidence; это всегда опционально и никогда не влияет на rerun.
18. GitHub repository↔plugin identity и payload одной SemVer неизменяемы; exact version не предлагается повторно, downgrade/unknown/conflict блокируются.
19. Каждый release target и каждый plugin проходят smoke из установленного artifact на всех заявленных OS/architecture; Windows runtime не зависит от внешних Python/Node/VC++.

Эта честность — часть продукта: Hub должен не только запускать много софтов, но и показывать, где операция завершена, где требует внимания и чему именно пользователь доверил ключи.
