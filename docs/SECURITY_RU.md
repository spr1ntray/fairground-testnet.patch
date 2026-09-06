# Безопасность Soft Hub MVP

Этот документ описывает фактическую модель безопасности Soft Hub `0.6.22`. Это не заявление об аудите и не гарантия сохранности средств. Текущие macOS arm64 и Windows x64 desktop builds — локальные preview для одного оператора и доверенных плагинов, а не публичный production-релиз; использование ценных mainnet-ключей до независимого аудита не рекомендуется.

## Коротко

- Hub слушает только `127.0.0.1` и защищает API случайным токеном текущего запуска.
- Account secrets, включая email password, Twitter, AdsPower profile ID и ссылку `child → direct parent`, а также глобальные Capsolver/AdsPower API keys зашифрованы в SQLite с помощью AES-256-GCM; ключ получается из мастер-пароля через scrypt. Hub не просит и не сохраняет project referral codes; после plugin fetch exact value лишь кратко проходит через память host в неперсистируемом `protect_secret` control-frame.
- Закрытый Vault скрывает account/run/account-state/result/event/log projections и configured-статусы глобальных ключей; renderer очищает защищённый client cache. Остаются только безопасные aggregate counts активных/требующих внимания runs.
- Plaintext export требует открытый Vault и повторную проверку мастер-пароля; Excel-safe XLSX не исполняет formula-like ячейки, но скачанный файл всё равно больше не защищён Vault.
- Плагин получает только типы секретов из exact grant выбранного action; обязательные `actions[].resources` проверяются до создания subprocess.
- Run options повторно валидируются core по закрытой schema; неизвестные поля, неверные типы, enum/range и пропущенные required отклоняются до доступа к Vault.
- Рекомендуемый manifest `/5` отделяет необязательные metadata размещения и UI-подсказки от risk, chain и permission checks; строгий `/4` остаётся совместимым.
- Архив проверяется на path traversal, symlink, конфликтующие пути, размер и SHA-256 файлов.
- Desktop release не содержит встроенных софтов или каталога адаптеров; чистый Hub начинает с нулём модулей, а локальная/GitHub-установка всегда является явным решением оператора.
- Packaged app запускает core через собственный управляемый Python runtime с `-I`; Python/Node.js пользователя не входят в runtime-требования.
- **Python-плагин является доверенным кодом.** Subprocess и `.venv` не являются security sandbox.
- Плагины имеют статус `local_unsigned`. macOS preview подписан ad-hoc и не notarized, Windows EXE не подписан Authenticode; ни plugin checksums, ни эти артефакты не доказывают личность издателя.

## Активы и границы модели

Hub защищает следующие активы от случайной утечки и части локальных атак:

- EVM private keys;
- HTTP proxy credentials;
- email и опциональные email passwords;
- Twitter credentials;
- глобальный Capsolver API key;
- AdsPower profile ID и глобальный AdsPower API key;
- зашифрованная топология связей между Hub-аккаунтами и project-specific referral codes, которые доверенный плагин получает и временно держит в памяти plugin/host текущего run;
- мастер-пароль и производный ключ vault;
- история запусков, результаты и локальная конфигурация;
- целостность установленного содержимого пакета относительно его `hub.checksums.json`.

NFT, approvals, marketplace orders и сами on-chain активы не хранятся в Vault, но находятся под контролем EVM private key и подписей, которые может создать доверенный плагин. Поэтому компрометация ключа или слепая mainnet-подпись может привести к потере NFT и токенов, даже если SQLite остаётся зашифрованной.

Модель предполагает:

- одного доверенного пользователя за локальной сессией ОС;
- некомпрометированную ОС и учетную запись пользователя;
- доверенный исходный код самого Hub;
- ручную проверку происхождения каждого плагина и его зависимостей;
- отсутствие публикации loopback-сервиса через reverse proxy, port forwarding или измененную сетевую обвязку.

MVP не защищает от администратора/root, malware под тем же OS-пользователем, чтения памяти процесса, кейлоггера, вредоносного проверенного пользователем плагина или компрометированного Python-пакета из `requirements.txt`.

## Рассматриваемые угрозы

Реализованные проверки уменьшают риск следующих сценариев:

- вредоносный ZIP пытается записать файл за каталог версии, подложить symlink или конфликтующий по регистру путь;
- zip bomb исчерпывает диск или память;
- посторонняя web-страница отправляет запросы к локальному API;
- случайный `print`, traceback или structured result раскрывает выданный плагину private key, JWT либо proxy;
- два write-запуска одновременно используют один аккаунт в одной chain;
- Hub перезапускается во время незавершенного write-действия;
- плагин отправляет поврежденные или произвольные protocol frames.
- вредоносный пакет маркирует себя как NFT/Testnet, чтобы выглядеть безопаснее, но фактически отправляет mainnet write;
- поддельная WL/OpenSea-страница просит unlimited approval, permit, marketplace order или произвольную подпись;
- RPC сообщает ожидаемый интерфейс, но фактический chain/contract/calldata/value отличается от заявленного.

Остаточные риски подробно перечислены ниже. Особенно важно: декларация разрешений не ограничивает системные вызовы Python-процесса.

## Vault

### Что хранится зашифрованно

Для каждого аккаунта Hub сериализует `evm_private_key`, полный проверенный Solana keypair `solana_private_key`, полный proxy, email, email password, Twitter, AdsPower profile ID и единственное реферальное поле `referrer_account_id` в один JSON payload и шифрует его `AESGCM` со случайным 12-byte nonce. Associated data привязывает ciphertext к ID аккаунта и версии формата. Подмена или повреждение ciphertext приводит к ошибке аутентификации.

Solana-значение принимается только как полный 64-byte Ed25519 keypair: canonical base58 либо JSON-массив Solana CLI из 64 целых байтов. Hub проверяет, что публичная половина действительно соответствует seed, и отклоняет 32-byte seed, публичный адрес, mnemonic, hex и двусмысленные формы. В SQLite открыто хранится только boolean `solana_configured` и SHA-256 fingerprint для защиты от случайного дублирования; ни ключ, ни публичная половина не возвращаются в bootstrap/UI. Плагин получает canonical base58 только при exact grant `solana_private_key`.

При первом успешном unlock после обновления Hub одноразово расшифровывает все account payloads, удаляет legacy-поля `referral_code` и `external_referrer_code`, сохраняет только валидные `referrer_account_id`, проверяет весь граф и заново шифрует payloads в одной транзакции. Migration marker не ставится до полного успеха. Это намеренный безвозвратный scrub старых кодов.

Глобальные Capsolver и AdsPower API keys хранятся отдельно в `vault_secrets`, каждый шифруется тем же derived key с отдельным nonce и AAD, привязанным к имени секрета. Каждый API key должен содержать минимум `4` символа: это fail-closed граница для корректной exact-value redaction, а не оценка силы ключа. При открытом Vault Bootstrap/UI возвращают только `capsolver_configured` и `adspower_api_configured`; при закрытом — `null`, чтобы не раскрывать даже факт настройки. Значение выдаётся run только при соответствующем exact action grant.

256-bit ключ получается из мастер-пароля через scrypt со случайной 16-byte salt и текущими параметрами `N=32768`, `r=8`, `p=1`. Пароль не сохраняется. В БД сохраняются salt, параметры KDF и зашифрованный verifier, необходимый для проверки пароля.

При unlock Hub принимает только этот точный поддерживаемый KDF profile, 16-byte salt и мастер-пароль не более 4096 UTF-8 bytes. Подмена `kdf_json` на ресурсоёмкие или неизвестные параметры завершается fail-closed до запуска scrypt.

Минимальная длина мастер-пароля — 14 символов; дополнительно отклоняются слишком однообразные строки. Это базовая проверка, а не оценка реальной энтропии. Используйте уникальную длинную passphrase и храните ее отдельно от data directory.

### Что остается метаданными

SQLite содержит в открытом виде данные, нужные интерфейсу и дедупликации:

- label и публичный EVM address;
- endpoint proxy без логина и пароля (`host:port`);
- маскированный email, включая домен;
- логический флаг `twitter_configured`, но не Twitter-значение;
- логические флаги `email_password_configured` и `adspower_configured`, но не пароль и не AdsPower profile ID;
- логический флаг `solana_configured` и fingerprint, но не Solana keypair;
- tags, статусы и timestamps;
- несоленые SHA-256 fingerprints EVM private key, полного Solana keypair, proxy и email;
- манифесты, события, результаты и пути установленных плагинов.

Fingerprints не раскрывают private key напрямую, но позволяют подтверждать догадку о значении с низкой энтропией — особенно об email или proxy. Поэтому backup базы тоже считается чувствительным.

### Жизненный цикл ключа

После create/unlock производный ключ находится в памяти процесса Hub до ручной блокировки или завершения процесса. `lock()` перезаписывает основной `bytearray` и удаляет ссылку, но Python и криптографические библиотеки могут создавать временные копии; нет `mlock`, secure enclave или гарантированного memory zeroization.

Desktop-оболочка считает `lock-screen`, `suspend`, слишком большой разрыв heartbeat, `resume` и `unlock-screen` единой fail-closed границей. Она сначала синхронно скрывает все окна и уничтожает renderer context, затем добивается idempotent lock ядра. Если живое ядро не отвечает, оболочка завершает именно свой дочерний core, запускает свежий закрытый core и только после подтверждённой блокировки загружает новый renderer с экраном пароля. После возврата gate не снимается от самого факта resume: он ждёт новой успешной разблокировки Vault. Это не может отозвать секрет, уже переданный работающему plugin process.

Блокировка является также границей проекций. Bootstrap возвращает пустые `accounts`, `runs` и `results`, нулевые account/result counters и не раскрывает configured-статусы глобальных ключей. Отдельные routes account list, runs, run-account states, results, run detail, events и technical log отвечают `423 Locked`. То же относится к stop/force-stop/review и операциям с account/global secrets. Любой POST одиночного или batch-start после того, как существующий Vault закрыт, также возвращает `423`: новые run IDs, leases и run projections до unlock не создаются/не выдаются. Account-free action без секретов не обходит эту host-границу после создания Vault.

Renderer при lock синхронно очищает собственные массивы accounts/runs/results/activity/events, закрывает панели/модальные окна и увеличивает epoch, поэтому in-flight response, начатый до lock, не может повторно заполнить DOM. В desktop-режиме shell, навигация и даже безопасные агрегаты полностью скрыты отдельным password gate до нового unlock; пользователь не может продолжить просмотр старой DOM-проекции. Если любой обычный renderer-запрос получает `423`, он применяет ту же границу, а не только показывает toast.

При запуске выбранные секреты расшифровываются и один раз передаются дочернему процессу JSON-строкой через `stdin`. После этого Hub очищает основные Python-контейнеры best effort. Это не signing broker: разрешенный плагин получает сам private key и может сохранить или отправить его.

В MVP нет смены мастер-пароля, recovery key или встроенного зашифрованного backup/restore Vault. Потерянный пароль восстановить нельзя. Для нового мастер-пароля нужен новый data directory и повторный импорт из отдельного доверенного источника.

Блок реферальных связей редактируется полным `child_account_id → parent_account_id|null` batch в одной SQLite-транзакции. Backend требует ровно одну запись на каждый текущий account, отклоняет duplicate/unknown/self-reference и любой цикл. `expected_revision` сравнивается через constant-time CAS до записи: устаревший UI не затирает параллельное изменение. Неудача откатывает весь batch. List API возвращает только safe parent ID/label, depth, root flag и child count. Повторный импорт известного private key сохраняет parent-связь; удаление parent-аккаунта атомарно очищает связь у его прямых children.

Редактор аккаунта использует `PATCH /api/accounts/{uuid}` с closed body `expected_revision + changes`. Renderer получает только публичную проекцию: EVM address, proxy endpoint, masked email и configured flags, включая `solana_configured`. Существующие EVM/Solana private keys, proxy credentials, email, email password, Twitter credential и AdsPower profile ID не возвращаются в DOM или client state. Каждое поле меняется только явной операцией `set`; необязательные значения удаляются только через `clear`. Backend нормализует данные, проверяет дубли, сравнивает account revision и выполняет merge + повторное AES-GCM шифрование в одной `BEGIN IMMEDIATE` транзакции. `run_account_pins` и active leases запрещают изменение аккаунта, который уже входит в согласованный run snapshot. Ответ содержит только новый revision и названия изменённых полей, не их значения.

### Plaintext account export

Export аккаунтов — явная declassification-операция, а не зашифрованный backup. Backend выполнит её, только если:

1. Vault уже разблокирован.
2. Повторно введённый мастер-пароль проходит verifier.
3. Отдельной подтверждающей фразы нет: кнопка сразу начинает экспорт после проверки пароля.

Ответ имеет `Cache-Control: no-store`; account export содержит `private_key,proxy,email,twitter,adspower_profile,solana_private_key`, поэтому считается plaintext secret export целиком. Глобальные Capsolver/AdsPower API keys, email passwords и топология связей не экспортируются; реферальные коды Hub не persist-ит. Поэтому этот plaintext-файл одновременно опасен как declassified subset и недостаточен как backup: повторный импорт в новый Vault не восстановит реферальную сеть. Эти проверки не защищают уже созданный plaintext-файл: его нельзя коммитить, отправлять в чат или оставлять в незашифрованном Downloads.

### Статистическая проекция и CSV

`output.mode: "account_table"` не разрешает плагину произвольный UI и не расширяет его permissions. Манифест объявляет не более 12 прямых scalar-ключей и до четырёх числовых агрегатов. Backend формирует safe report projection только из этих allowlisted keys результата с `kind`, равным snapshot `primary_kind`; необъявленные поля, вложенные объекты/массивы, raw response, DOM/HAR и plugin-owned HTML в UI не попадают.

Label и public EVM address связывают действия одного оператора между сервисами, поэтому считаются чувствительной метаданной, хотя и не являются private key. Просмотр, поиск, фильтр и CSV export требуют token-authenticated API и открытый Vault; при lock server отвечает `423`, renderer очищает проекцию, а запоздавший ответ не может вернуть строки на экран.

Итоговый статус берётся из authoritative `run_account_states`, а не из title, log или произвольного plugin status. Схема таблицы и account identity фиксируются в snapshot запуска, чтобы update/delete не переопределили историю. Сохранённые title/data повторно проходят redaction при построении safe projection; это defense-in-depth, а не разрешение плагину эмитить секрет.

CSV формирует renderer только из уже полученной полной safe report projection с текущими поиском и фильтром. Он не обращается к raw results и не добавляет необъявленные поля. Если backend сообщает `truncated` — запуск содержит больше лимита 2 000 строк — renderer блокирует CSV целиком, а не экспортирует неполную выборку. Строковые ячейки, начинающиеся с `=`, `+`, `-`, `@`, tab, carriage return или line feed, получают ведущий апостроф и сохраняются как текст. Schema-typed `integer`, `number` и `decimal_string` не получают апостроф: отрицательные значения остаются числами. Кнопка скачивания доступна только при открытом Vault; после download файл становится обычным plaintext-артефактом и требует ручной проверки перед передачей третьему лицу.

На POSIX Hub пытается создать data directory с mode `0700`, а базу и файлы пакета — с ограниченными mode. На Windows используются ACL, унаследованные от профиля пользователя; отдельная настройка ACL кодом не выполняется.

В macOS desktop data directory по умолчанию находится в `~/Library/Application Support/Soft Hub`, отдельно от `/Applications/Soft Hub.app`. Замена или удаление только `.app` не стирает Vault и историю. Это удобная граница обновления, а не security boundary: процесс с доступом к каталогу пользователя или его backup всё равно получает ciphertext и открытые metadata.

## Loopback API, токен и CSP

HTTP-сервер создается на `127.0.0.1` со случайным свободным портом. Он не должен быть доступен из LAN. Проверка `Host` принимает только `127.0.0.1` и `localhost`; изменяющие запросы дополнительно проверяют `Origin`, если браузер его прислал. `OPTIONS` отключен.

На каждом старте создается `secrets.token_urlsafe(32)`. Токен передается UI в URL fragment, считывается JavaScript, сохраняется в `sessionStorage` и сразу удаляется из адресной строки через `history.replaceState`. Все `/api/*` запросы обязаны передать его в `X-Soft-Hub-Token`; сравнение выполняется через `compare_digest`. Перезапуск Hub аннулирует старый токен.

Fragment обычно не отправляется HTTP-серверу и не попадает в Referer. Тем не менее токен доступен JavaScript-контексту страницы и любому, кто контролирует браузерную сессию или может читать память/хранилище того же пользователя. Не публикуйте стартовую строку `SOFT_HUB_READY`, не копируйте полный URL и не оставляйте терминал доступным посторонним.

Ответы получают `Cache-Control: no-store` и security headers: строгий CSP с ресурсами только от `'self'`, запрет object/base/frame, `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer` и same-origin resource policy. CSP уменьшает browser-атаку, но не защищает от измененного локального frontend-кода или вредоносного плагина под тем же OS-пользователем.

Electron renderer использует `contextIsolation`, отключенный `nodeIntegration`, включенные `sandbox`, `webSecurity` и запрет навигации на другой origin. Только явный CTA Patch Radar может открыть через системный браузер заранее проверенный HTTPS URL вида `github.com/<owner>/<repo>`; само Electron-окно наружу не переходит. Это относится к UI renderer, а не к Python-плагинам.

TLS и пользовательская аутентификация отсутствуют, потому что сервер задуман только для loopback. Не меняйте bind address на `0.0.0.0` и не выставляйте API наружу.

## Пакет плагина

При установке Hub проверяет:

- максимальный размер архива 256 MB, распакованного содержимого 512 MB и максимум 4000 entries;
- отсутствие абсолютных путей, `..`, backslash, NUL, Windows device names, непереносимых имен и symlink;
- отсутствие точных и case-insensitive конфликтов путей;
- подозрительный compression ratio для файлов больше 1 MB;
- наличие в корне `hub.plugin.json` и `hub.checksums.json`;
- совпадение списка файлов с checksums и SHA-256 каждого payload-файла;
- запрет известных credential/session путей: `.env`, HAR/cookies, private key, proxy/email/account списков, certificate keys и локальных DB; тот же denylist применяется штатным builder до создания архива;
- базовый контракт manifest v1, минимальную версию Hub, обязательные risk/actions поля и exact action grants/resources; строгие `/4` catalog/UI-правила либо безопасные `/5` defaults не заменяют проверку кода.

Распаковка идет во временный staging-каталог, затем версия переносится в `plugins/<id>/<version>`. Обновления идут только на более новую SemVer: downgrade и повторная активация прежней версии недоступны в UI, API и `PluginManager`. Exact текущая версия с тем же SHA-256 считается уже установленной; та же SemVer с другим содержимым блокируется.

Строки `module_versions` сохраняют неизменяемую связку `id + SemVer + archive SHA` и GitHub source identity между обновлениями и после удаления модуля. Uninstall в 0.6.22 стирает код и `.venv`, помечает версии неактивными, но оставляет эти невидимые identity-tombstones: старую SemVer нельзя активировать снова, а привязанный `plugin id` нельзя перенести в другой repository. Identity-записи, уже удалённые прежней версией Hub до обновления, автоматически не восстанавливаются. История запусков отдельно сохраняется через собственные snapshots `runs`, результаты и события; это не механизм возврата на прежний код.

Это защита целостности и безопасной распаковки, **не проверка происхождения**. `hub.checksums.json` находится в том же неподписанном архиве, поэтому автор вредоносного пакета может пересчитать все суммы. Текущий `trust_status` — `local_unsigned`. Нет подписи издателя, certificate chain, transparency log или allowlist доверенных ключей.

### GitHub Release и Patch Radar

Обычная GitHub-установка и Patch Radar в 0.6.22 работают только с public GitHub-данными без token/Authorization. Private repositories не видны, а tokens/credentials в URL отклоняются. Это также означает анонимные GitHub API rate limits.

Patch Radar принимает только username или точный HTTPS owner URL, читает не более первых 100 public repositories с `api.github.com`, отбирает точный case-insensitive суффикс `.patch` и читает только `releases/latest`. Ready-статус возможен лишь при ровно одном release asset с case-insensitive суффиксом `.softhub` или `.softhub.zip` и безопасной GitHub download URL того же owner/repository. Ранняя metadata-проверка требует, чтобы `asset.size` был целым числом от 1 byte до 256 MB, а `browser_download_url` — строкой не длиннее 2048 символов.

Само сканирование выполняет только ограниченные metadata GET: оно не скачивает, не открывает, не проверяет, не устанавливает и не запускает asset. Скачивание начинается только по явной команде оператора; downloader независимо применяет собственный лимит 256 MB, после чего архив проходит обычный install pipeline.

После первой проверенной установки таблица `github_module_sources` привязывает repository к фактическому module id, а release asset — к версии и SHA-256. Core блокирует смену module id в том же repository, перенос связанного id в другой repository, downgrade и повторную публикацию той же версии с другим содержимым. Неясная SemVer metadata и identity conflicts не получают автоматическую кнопку Radar. Привязка берётся только из server-side downloader и inspected manifest, а не из renderer. Она защищает последовательность обновлений, но по-прежнему не доказывает личность или доверенность GitHub-автора.

### Обновление самого Hub

Desktop updater отделён от установки софтов и работает только с фиксированным публичным repository `spr1ntray/soft-hub`. Renderer не передаёт ему owner, repository, download URL, имя файла или локальный путь. После загрузки интерфейса updater проверяет latest stable release, а при долгой сессии повторяет metadata-проверку раз в шесть часов только при видимом окне; автоматической загрузки или установки нет.

Новая версия принимается только при строгом увеличении SemVer и `immutable === true` у GitHub Release. Единственное legacy-исключение — `v0.6.14`, опубликованный до включения repository immutability: updater содержит точный pin его release ID, target commit SHA, publish timestamp и всех трёх asset ID/size/GitHub SHA-256. Исключение не является общим разрешением mutable releases; любая разница в metadata блокирует кандидат, а фактический download всё равно обязан совпасть с закреплённым digest. Этот переходный pin удаляется после того, как первый immutable public release станет минимально поддерживаемой версией.

Требуется точный единственный asset текущей платформы: `Soft-Hub-<version>-arm64.dmg` для macOS arm64 либо `Soft-Hub-<version>-x64.exe` для Windows x64. Рядом обязан находиться единственный `SHA256SUMS` с точной строкой этого файла. GitHub-computed `sha256:` digest обязателен у обоих assets. До состояния `downloaded` проверяются platform/asset identity, заявленный и фактический размер, digest самого checksum manifest, SHA-256 скачанного файла и совпадение всех трёх представлений. Неполный, слишком большой, неоднозначный или изменённый asset удаляется из staging и не может быть открыт.

Скачивание начинается только после отдельного действия пользователя; открытие уже проверенного установщика требует второго явного нажатия, без кодовой фразы. Перед ним updater несколько раз проверяет отсутствие active runs, повторно хеширует cached installer и на commit-boundary сначала скрывает окна, уничтожает renderer context и блокирует Vault. После этого он ещё раз проверяет active runs и хеширует тот же exact path непосредственно перед OS launch. Любой recoverable отказ возвращает пользователя только через свежий экран мастер-пароля. На macOS открывается проверенный DMG, после чего пользователь вручную заменяет приложение в `Applications`; на Windows запускается проверенный NSIS EXE без shell-командной строки. Renderer не получает staging path и не может попросить запустить произвольный файл.

Immutable Release не позволяет заменить tag и assets после публикации, но это всё ещё доверие к GitHub-owner: захват аккаунта/PAT/сессии позволяет выпустить новую более высокую SemVer с согласованными вредоносными digest и `SHA256SUMS`. Поэтому встроенная проверка не отменяет offline-signed release manifest с публичным ключом внутри Hub, Developer ID + notarization на macOS и Authenticode на Windows. Текущие preview-сборки остаются неподписанными в публичном смысле. Updater не меняет настройки Gatekeeper или SmartScreen, однако custom download не рассматривает наличие quarantine/MOTW как доказанный инвариант: видимость системного предупреждения зависит от ОС и метки происхождения, а его отсутствие не подтверждает подпись или доверенность файла.

## Граница доверия плагина

Плагин запускается отдельным subprocess без shell, с отдельной process group, очищенным набором environment variables, рабочим scratch-каталогом и, при наличии зависимостей, отдельным `.venv`. Эти меры уменьшают случайные конфликты, упрощают остановку и не дают автоматически наследовать большинство переменных окружения.

Они **не создают границу безопасности**. Процесс работает от того же OS-пользователя и без filesystem/network sandbox. Он может обращаться к любым файлам, сокетам, процессам и устройствам, которые разрешены этому пользователю и ОС. Отдельный `.venv` изолирует Python dependencies, но не полномочия.

Фактическое значение manifest permissions в MVP:

| Поле | Что реально делает Hub | Чего оно не делает |
|---|---|---|
| `permissions.secrets` + `actions[].permissions.secrets` | Top-level показывает union плагина, а runner передаёт только точный набор выбранного action; legacy manifest получает top-level fallback | Не запрещает плагину самостоятельно читать доступные ему файлы или сеть |
| `actions[].resources` | До run/spawn проверяет наличие обязательных account/global значений и соответствие grant; ошибка указывает ресурс и аккаунт | Не проверяет доступность внешнего сервиса и корректность credential у провайдера |
| `permissions.chains` | Создает account leases для write-действий | Не проверяет RPC URL, chain ID транзакции или содержимое подписи |
| `actions[].risk` | Отличает read, non-financial `external_write` и chain writes; выбирает предупреждения и тип leases перед запуском | Не определяет фактическое поведение кода и не доказывает итог внешней операции после сбоя |
| `catalog.sections` / derived `catalog_sections` | Валидирует форму placement и сохраняет immutable run snapshot; `/4` дополнительно cross-checks testnet/mainnet, `/5` допускает независимое или отсутствующее placement | Не доказывает NFT, фактическую сеть/risk и не выдаёт permission. Злонамеренный код может солгать о поведении |
| `runtime.safe_stop` | Разрешает UI отправить stop; после 10 секунд возможен force kill | Не гарантирует транзакционность или возможность безопасного восстановления |
| `compatibility.hub` | Проверяет минимальную поддерживаемую версию Hub | Не является подписью или sandbox policy |
| `permissions.network`, `browser`, `local_services`, `financial_risk` | Являются декларативными метаданными; AdsPower grants требуют `browser=true` и canonical local service `adspower` | Не образуют принудительный allowlist или запрет системных вызовов |
| `compatibility.python`, `compatibility.os` | Описывают ожидаемую среду; значения OS проходят синтаксическую проверку | Текущий MVP не гарантирует runtime enforcement заявленного диапазона/платформы |

Устанавливайте плагин только если вы готовы выполнить его код с правами своего пользователя и выдать ему все заявленные секреты. Проверяйте не только исходники архива, но и каждую зависимость `requirements.txt`: команда **Подготовить** запускает `pip install`, а install hooks и импортируемые пакеты являются частью trust boundary.

Referral-aware action контрактов `SH-SOFTWARE-0.6/4` и `/5` может объявить `action.referral.mode: "project_runtime"`, `parent_required`, `parent_access` и отдельные exact `permissions.secrets`/`resources.account` для direct parent. Legacy-имена secret grants `referral_code`/`referrer_code` и одноимённые resources запрещены: Hub не хранит такие коды в Vault. `/4` дополнительно запрещает ручной referral/invite option как жёсткое бизнес-правило; `/5` не навязывает проекту этот workflow и разрешает автору описать обычный bounded option, если это действительно вход сценария и не секрет Vault. Runner фиксирует топологический revision при admission, выдаёт только уникальных direct parents выбранных targets и только объявленные для parent секреты. `parent_access: "exclusive"` добавляет service-lease; `shared_read` его не создаёт. Targets и выданные parents записываются в `run_account_pins`, поэтому их нельзя удалить до завершения run. Это exact grant данных, но не OS sandbox.

Плагин берёт parent только из `context.referrals.parent_for(child.id)` или bounded набора `context.referrals.parents`, сам авторизует его в целевом проекте и получает project-specific code. Сразу после получения и до любых log/result/exception/`print` он обязан вызвать `context.protect_secret(code)`. Exact value передаётся как неперсистируемый control-frame: он кратко находится в памяти plugin/host текущего run и регистрируется в Redactor, но не входит во входной context/options и не сохраняется как event, result, summary, exception, URL, файл или cache. Bootstrap после decode также оборачивает plugin stderr/redirected stdout локальным `context.sanitize_text`, снижая риск race между frame и случайным `print`; это не защищает вывод до вызова, split/custom encoding, screenshots, files или сеть. Raw `print`/logging кода всегда запрещён.

### NFT и Testnet catalog

Raw `catalog.sections` и derived `catalog_sections` являются открытой manifest/run metadata, а не секретом Vault. При admission Hub сохраняет effective sections в `runs.catalog_sections_json`; locked boundary всё равно скрывает сами run/result projections, но не обязан скрывать безопасные агрегаты разделов.

Для `/4` validator не допускает `general` вместе с другим section, требует `testnet` для `testnet_write` и запрещает testnet section при mainnet risk. В `/5` catalog необязателен и не связан с action risk: без него fallback относит testnet-risk в `testnet`, остальное в `general`, а указанное размещение остаётся только UI-метаданными. NFT по тексту не угадывается. Ни один из этих вариантов не анализирует код, RPC или транзакцию. Раздел **NFT** не подтверждает официальный контракт/OpenSea domain, а раздел **Тестнеты** не доказывает фактический chain ID.

WL submit является как минимум `external_write`. Mainnet mint, approval, листинг, продажа, transfer и off-chain marketplace/order signature являются `mainnet_write`, даже если немедленной транзакции нет. Они допускаются в batch, но режим пачки не снижает риск и не обходит exact grants, Vault/preflight или account/scope leases. Плагин обязан до подписи проверить chain, contract, calldata/recipient/value, fee bounds, token/spender/approval и точный order intent; пользователь отдельно проверяет те же публичные параметры в независимом источнике.

## Запуски, остановка и журналы

Hub принимает от bootstrap только JSONL frames протокола `soft-hub-jsonl/1` с известными event types. После трех malformed frames процесс завершается принудительно; line и общий объём строк ограничены. Любой непустой `account_id` дополнительно должен принадлежать выбранным аккаунтам именно этого run; чужой ID не записывается в events/results. Для chain-write действий lease не даёт параллельно использовать тот же account и chain; `external_write` получает отдельный per-account service-lease. Оба вида продлеваются независимо от частоты вывода.

Выбранный набор аккаунтов и их lifecycle сохраняются отдельно в `run_account_states`. Terminal status меняет только типизированный `account_state`, а не текст последнего log/result. Это защищает интерфейс от ложного «успешно» в произвольном сообщении; отсутствие итогового события не превращается в успех. В projection попадают только публичные ID/label и уже отредактированный message, но labels и сама БД всё равно считаются чувствительными метаданными.

Для `testnet_write` отдельного checkbox или подтверждающей фразы нет: явное нажатие кнопки запуска является пользовательским действием. Runner по-прежнему принудительно добавляет в context `options.acknowledge_testnet_transactions=true`, если action schema объявляет такое legacy boolean-поле; клиент не управляет этим служебным значением. Плагин не должен рисовать собственное подтверждение.

Non-financial мутация внешнего API объявляется как `external_write`, а не `read`. Для неё нет financial acknowledgement, однако runner выдаёт write-lease на время активного запуска. После force stop или падения run становится `cancelled` либо `failed`, lease освобождается, но это не доказывает отмену внешней операции: HTTP-сервер мог принять запрос до разрыва локального процесса.

При любом terminal завершении run — в том числе `failed` и `cancelled` — Hub освобождает account/service leases и pins. Отдельного подтверждения внешней сверки нет, завершённый run не удерживает аккаунт и не блокирует повторный запуск. Это решение касается только внутренней координации Hub: он по-прежнему не может доказать, была ли транзакция или запись отправлена до сбоя, поэтому оператор обязан проверить explorer/API перед опасным повтором. При запуске новой версии исторические `needs_attention` нормализуются в `failed`; исходные error, events, results и журнал сохраняются как legacy-след.

Для terminal-проблемы оператор может открыть или скачать очищенный общий журнал, а затем закрыть уведомление: run получает `reviewed` и исчезает только из live-attention projection. Review не меняет исходную ошибку, account states, results или events и не выдаёт запуск за успешный. Повторный запуск не зависит от review: аккаунты освобождаются уже при переходе исходного run в terminal status.

Force-stop является отдельной немедленной операцией и запускается одним нажатием без acknowledgement-фразы. Это завершение локального дерева процессов, а не откат внешнего действия: уже принятый внешний запрос может выполниться. Run завершается как `cancelled`, Hub освобождает leases, а внешний итог остаётся виден в журнале и результатах.

Queued run, который ещё не получил concurrency slot или ждёт конфликтующий account lease и не создал subprocess, также замечает safe/force cancellation до расшифровки account bundle и spawn, завершается как `cancelled` и освобождает pins/lease. Один и тот же инвариант действует для любого terminal run: внутренние leases и pins не переживают завершение процесса.

Пакетный запуск принимает пачку только одним backend-запросом с `execution_mode: parallel|sequential`: полный closed `options` каждого выбранного action и account revisions проверяются для каждого элемента, а persistent idempotency key учитывает режим и предотвращает дубли после неопределённого сетевого ответа. Mainnet actions допускаются, но проходят те же risk/grant/resource checks, что и одиночный запуск. Renderer хранит отдельный draft параметров на `module id + module version + action id` и отправляет его в соответствующий run; `account_concurrency` также остаётся per-run option, без общей скрытой копии. На время запроса вся batch-форма замораживается, а неизменяемый payload использует один key до ответа; после сетевой ошибки повтор без правок сохраняет тот же key. Batch, runs, account states и pins создаются одной транзакцией; пачка либо целиком поставлена в очередь, либо не создаёт ни одного run. Эта атомарность относится к admission в Hub; уже запущенные внешние операции общей транзакцией не становятся.

В `parallel` write/exclusive leases выдаются непосредственно перед каждым выполнением. Если scope/account занят, worker освобождает глобальный subprocess slot, остаётся queued и повторяет попытку; поэтому конфликтующий софт ждёт автоматически, а несвязанные задачи используют свободную ёмкость. В `sequential` следующий пункт ждёт terminal-состояния **всех** более ранних пунктов. Ошибка или отмена раннего run не отменяет остаток пачки, но более поздний run не может обогнать ещё работающий первый. Такой waiter не занимает subprocess slot, не удерживает account lease и не получает расшифрованный Vault payload. Это уменьшает время жизни секретов и ресурсов, но не делает последовательные внешние операции транзакцией и не отменяет уже случившийся side effect.

Hub разделяет concurrency двух уровней. Глобальный `--max-concurrent` ограничивает число plugin subprocess, а action option `account_concurrency`, если софт его объявил, — workers внутри одного subprocess. Строгий `/4` требует это поле для account-action; свободный `/5` может положиться на безопасный последовательный default. Core валидирует integer, ограничивает `/5` универсальным пределом `20` и зажимает effective value по числу targets; строгий `/4` дополнительно ограничивает browser schema значением `5`. Для browser-плагина `/5` меньший фактический предел обязан выбрать и проверить автор. Это не rate limiter и не nonce manager: плагин всё равно обязан учитывать provider limits, per-account изоляцию, finite timeouts, cancellation и thread safety своих clients/cache.

При штатном shutdown Hub сначала прекращает admission, сигналит активным plugin processes, ждёт grace period и принудительно завершает оставшиеся до lock Vault. На POSIX Hub очищает всю process group, а на Windows удерживает дерево в Job Object с `KILL_ON_JOB_CLOSE`; `taskkill /T /F` остаётся только резервным путём. Намеренно detached-процесс и уже принятый внешним сервисом side effect всё равно нельзя считать доказанно отменёнными.

Redactor получает точные значения и account bundles, и `HubSettings`, затем заменяет выданные секреты, secret-поля вложенных объектов, authorization/cookie headers, email, password/token/API-key assignments и строки, похожие на EVM private key, JWT, proxy или длинный credential. Project runtime values регистрируются только после `protect_secret`; bootstrap дополнительно очищает последующие text/binary writes через обёртку stderr. Он также ограничивает глубину, количество элементов и длину сообщений. Технический журнал скачивается только через token-authenticated API как один bounded UTF-8 JSONL-файл конкретного run: он содержит общий поток всех аккаунтов с безопасными account labels, но не manifest/options; сохранённые события повторно проходят redaction непосредственно перед export. Ответ имеет `Cache-Control: no-store`.

Это всё ещё best effort, а не DLP: Redactor не обнаружит все кодировки, фрагменты, хэши, файлы, изображения или нестандартно разбитые секреты. Плагин обязан никогда не помещать secrets в `message`, `data`, exception text, имя файла или собственные логи. Скачанный `.log` остаётся чувствительным артефактом истории и не должен публиковаться без проверки.

Scratch-каталог сохраняется после run. Если плагин записал туда секрет, он останется на диске вне зашифрованного vault. Hub пока не очищает и не сканирует scratch автоматически.

## Практика работы с секретами

- Для dev/test используйте отдельные пустые кошельки, тестовые email и proxy credentials.
- Не коммитьте private keys, `.env`, входные или экспортированные TXT/CSV/TSV, готовые пакеты с секретами, data directory или backup.
- Не вставляйте секреты в issue, мессенджер, AI-чат, скриншот, HAR, traceback и console output.
- Выдавайте плагину минимальный набор `permissions.secrets`; read-action без секретов должен использовать пустой список.
- Не добавляйте секреты в manifest, options или `requirements.txt` — эти данные не шифруются как account payload.
- Блокируйте vault после работы и полностью завершайте Hub на общей машине.
- Храните мастер-пароль отдельно от backup. Проверяйте возможность восстановления на тестовой копии.
- Делайте backup только при остановленном Hub и копируйте весь data directory, чтобы не получить несогласованный SQLite/WAL snapshot.
- Перед mainnet-запуском вручную проверяйте chain, RPC, адреса контрактов, лимиты, allowance и ожидаемый баланс в независимом источнике.
- Для testnet используйте отдельные пустые кошельки без mainnet-активов, старых approvals и ценных NFT.
- Перед NFT/WL workflow проверяйте официальный домен проекта/OpenSea, contract, chain ID, цену/`value`, gas/fee, количество и точный approval/order. Раздел приложения не является знаком доверия.
- Не подписывайте unlimited approval, permit, arbitrary message или marketplace order, если точный эффект не выделен в отдельный проверенный `mainnet_write` action.
- Фиксируйте версии зависимостей; для критичных плагинов используйте проверенные hashes/локальный wheelhouse и воспроизводимую сборку.

## Реагирование на инцидент

Если есть подозрение на утечку или вредоносный плагин:

1. Остановите активные runs, завершите Hub и, если эксфильтрация еще возможна, отключите сеть машины.
2. Считайте скомпрометированными все секреты, выданные плагину, а при компрометации OS-пользователя — весь vault после последнего unlock.
3. Для EVM-ключа переведите токены и NFT на новый кошелек; отзовите token/NFT approvals, permits, sessions и делегированные права, отмените активные marketplace/OpenSea orders. Одной смены мастер-пароля недостаточно.
4. Смените proxy credentials, email password, Twitter credential, Capsolver/AdsPower API keys, выпущенные проектом referral codes и активные email/API sessions, которые могли быть выданы плагину во время run.
5. Сохраните для анализа копию data directory, hash исходного `.softhub.zip`, manifest, `requirements.txt`, run ID, timestamps и внешние transaction hashes. Делайте копию после остановки Hub и не открывайте ее на основной машине.
6. Не запускайте и не подготавливайте подозрительный пакет повторно. Удаление в UI стирает Hub-owned code, все версии и `.venv`, но сохраняет audit history/results и не отзывает уже раскрытые секреты. Активный run сначала нужно остановить.
7. Перезапустите Hub, чтобы сменился loopback API token. Если подозревается подмена frontend/core, переустановите приложение из доверенного DMG или EXE с проверенным SHA-256; разработчики могут восстановить его из проверенного исходника.
8. Сопоставьте упавшие и принудительно остановленные write-runs с chain explorer/API проекта и marketplace orders. Не повторяйте mint/list/sale/submit, пока не исключён предыдущий broadcast или принятый ордер.

Если скомпрометирован только мастер-пароль, но злоумышленник имел доступ к копии data directory, считайте account secrets раскрываемыми и все равно ротируйте их. Функции rekey в MVP нет: создайте чистый data directory с новым паролем и импортируйте уже замененные credentials.

## Ограничения текущего MVP

### Неподписанные пакеты и приложения

Плагины имеют статус `local_unsigned`. SHA-256 не доказывает авторство: злоумышленник может изменить package payload и пересчитать находящийся рядом checksum manifest.

Текущая локальная macOS-сборка использует ad-hoc подпись и не проходит Apple notarization. Ad-hoc подпись помогает macOS проверить внутреннюю согласованность code bundle, но не связывает его с проверенным издателем и не заменяет Developer ID. Поэтому Gatekeeper может заблокировать первое открытие preview. Windows EXE не имеет Authenticode-подписи, поэтому SmartScreen также может предупреждать или блокировать запуск.

Публичный релиз требует сертификата Developer ID Application, hardened runtime, Apple notarization/stapling на macOS и Authenticode signing на Windows. До появления такого pipeline распространяйте preview только по доверенному каналу вместе с внешним SHA-256. Не отключайте Gatekeeper или SmartScreen глобально ради неизвестной сборки.

### Управляемый Python runtime

Текущий packaged Electron app включает CPython 3.12.13, зависимости core и сам `soft_hub`. Builder использует явные targets `darwin-arm64` и `win32-x64`, загружает закреплённый runtime archive, проверяет SHA-256, exact `requirements-runtime.lock` и hash исходников. macOS target выполняет native import/crypto self-check; cross-target Windows проверяется статически, включая `python.exe`, Python/MSVC DLL и архитектуру всех native `.pyd`. `beforePack` запрещает упаковать Darwin runtime в Windows-приложение или наоборот.

Launcher в packaged-режиме рассматривает только `Contents/Resources/python/bin/python3` на macOS либо `resources/python/python.exe` на Windows, запускает его с `-I` и не использует `PATH`, системный Python или `SOFT_HUB_PYTHON` как fallback. Windows layout содержит нужные MSVC runtime DLL, поэтому отдельный Visual C++ Redistributable пользователю не требуется. Статическая cross-проверка не заменяет clean-machine install/start/import smoke на Windows.

Managed runtime всё равно является частью trust boundary: доверять приходится источнику CPython artifact, закреплённому hash, release builder, lock-файлу и включённым wheels. Self-check обнаруживает часть повреждений и несовместимостей, но не доказывает отсутствие вредоносного кода и не создаёт sandbox.

Системный Python и `SOFT_HUB_PYTHON` используются только при разработке/запуске из исходников; разработчику нужен отдельный `.venv`. Команда **Подготовить** создаёт `.venv` плагина на базе interpreter Hub и затем устанавливает его `requirements.txt`. Эти сетевые зависимости и их build hooks остаются отдельным supply-chain риском, даже когда core runtime встроен в приложение.

### Нет OS sandbox и signing broker

Нет container/VM, seccomp, App Sandbox, Windows AppContainer, filesystem allowlist, network egress filter, privilege separation или отдельного низкопривилегированного пользователя. Нет внешнего broker, который подписывает заранее проверенную транзакцию без выдачи ключа. Плагин с разрешением `evm_private_key` получает private key целиком.

### Другие отсутствующие механизмы

- нет multi-user access control, TLS, remote API или rate limiting для unlock;
- нет публичного Developer ID/notarized macOS и Authenticode-signed Windows release pipeline;
- нет автоматической очистки scratch, malware scanning и dependency audit;
- нет автоматического или зашифрованного backup/restore, rekey и recovery master password; plaintext XLSX/raw CSV export не заменяет эти механизмы;
- нет гарантии, что force stop откатит внешнее действие;
- нет аппаратного хранилища ключей и интеграции с OS keychain;
- нет проверки реального network destination/chain транзакции;
- нет независимого security audit для этой версии.

До устранения этих ограничений относитесь к Soft Hub как к удобному локальному оркестратору доверенного кода, а не как к изолированному хранилищу для запуска недоверенных автоматизаций.
