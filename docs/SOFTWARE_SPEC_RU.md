# Нормативное техническое задание на софт для Soft Hub 0.6.22

Статус документа: нормативный безопасный контракт для новых модулей и новых версий существующих модулей, устанавливаемых в Soft Hub 0.6.22. Он задаёт границу интеграции, а не business workflow конкретного проекта.

Рекомендуемая версия контракта: `SH-SOFTWARE-0.6/5`.

## 1. Нормативные термины и приоритет источников

В этом документе используются следующие термины:

- **ОБЯЗАН / MUST** — требование обязательно. Нарушение блокирует приёмку или выпуск.
- **ЗАПРЕЩЕНО / MUST NOT** — действие недопустимо. Нарушение блокирует приёмку или выпуск.
- **СЛЕДУЕТ / SHOULD** — требование выполняется по умолчанию. Отступление допускается только с записанным техническим обоснованием, тестом и одобрением владельца Hub.
- **МОЖЕТ / MAY** — допустимый вариант, не являющийся обязательным.

Новый пакет СЛЕДУЕТ выпускать с `"contract_version": "SH-SOFTWARE-0.6/5"` и `compatibility.hub: ">=0.6.22"`. Строгий `/4`, `/3`, `/2` и пакеты без marker продолжают загружаться по своим историческим правилам. Использовать старый контракт для обхода обязательных safety invariants ЗАПРЕЩЕНО.

Если этот документ расходится с фактическим исполняемым контрактом Hub, выпуск останавливается: схема, валидатор, SDK, тесты и этот документ должны быть приведены к одному состоянию. Нельзя обходить расхождение кодом плагина.

Для нового пакета порядок источников истины таков: этот документ `/5`, `schemas/plugin.schema.json`, `validate_manifest()`, SDK/runtime и acceptance tests. Совместимость со строгим `/4` не превращает его project-specific ограничения в обязательную политику `/5`.

Исполняемые источники истины для Soft Hub 0.6.22:

- `soft_hub/plugins.py` — проверка манифеста, архива, presentation assets и установка;
- `schemas/plugin.schema.json` — авторская JSON Schema;
- `soft_hub/sdk.py` — публичный Python SDK;
- `soft_hub/runtime/bootstrap.py` — запуск entrypoint и JSONL-протокол;
- `soft_hub/runner.py` — preflight, lifecycle, выдача секретов, остановка, redaction, results и журнал;
- `scripts/build_plugin.py` — единственный штатный сборщик `.softhub.zip`.

Импорт внутренних модулей Hub, кроме `soft_hub.sdk`, ЗАПРЕЩЁН.

## 2. Цель и модель интеграции

Софт для Hub — это **полный, самодостаточный, неизменяемый пакет одной версии**, а не diff поверх предыдущей версии и не скрипт, который пользователь запускает вручную.

Soft Hub 0.6.22 поставляется без предустановленных софтов. Каждый рабочий модуль приходит отдельным пакетом и устанавливается пользователем из локального `.softhub.zip`, по ссылке на public GitHub Release либо через Patch Radar для подходящего `.patch`-репозитория. Примеры и исходники в репозитории Hub не являются встроенным каталогом и не должны появляться в чистой установке.

Hub — свободная безопасная песочница интеграции. Core НЕ ДОЛЖЕН содержать рецепты проекта, фиксированное число транзакций, порядок предметных этапов, обязательный способ получения referral code или список допустимых сценариев. Плагин сам объявляет actions, options, account/settings resources, risk и output. Hub отвечает только за универсальное отображение, preflight, минимальную выдачу Vault-данных, lifecycle процесса и безопасное сохранение результатов.

Пакет ОБЯЗАН позволять Hub:

1. показать название, полное описание, иконку и обложку;
2. заранее показать действия, опции, риск и необходимые ресурсы;
3. до запуска сообщить пользователю, каких данных или настроек не хватает;
4. запустить Python entrypoint без терминала и интерактивного ввода;
5. показать прогресс и итог отдельно по каждому выбранному аккаунту;
6. безопасно остановить работу и честно обозначить, если внешний итог нужно проверить перед повтором;
7. сохранить структурированные результаты и скачать очищенный технический журнал;
8. расположить карточку в общей библиотеке и, если объявлен catalog, в разделах `general`/`nft`/`testnet`, не угадывая назначение по тексту или коду;
9. обновить модуль новой полной версией без изменения контракта его постоянного `id`.

Плагин ЗАПРЕЩЕНО проектировать так, чтобы Hub разбирал произвольный текст терминала, угадывал обязательные поля формы, искал картинку по имени файла или анализировал исходный код. Всё, что действительно требуется интерфейсу и preflight, ОБЯЗАНО быть объявлено в `hub.plugin.json`; необязательные `/5` UI hints и catalog placement можно опустить, чтобы получить safe defaults.

## 3. Граница доверия

### 3.1. Что Hub обеспечивает

Hub:

- хранит account/global secrets в зашифрованном Vault at rest;
- передаёт subprocess только secret kinds, разрешённые выбранному action;
- запускает плагин без shell, в отдельной process group и с очищенным набором environment variables;
- использует отдельное Python-окружение версии плагина при наличии зависимостей;
- проверяет безопасную распаковку и SHA-256 каждого payload-файла;
- проверяет выбранные `account_id` в событиях;
- ограничивает протокол, объём вывода и размер скачиваемого журнала;
- редактирует события при сохранении и повторно при экспорте журнала;
- применяет risk gates и account leases для write-действий.

### 3.2. Чего Hub не обеспечивает

Плагин выполняется с правами текущего OS-пользователя. В Soft Hub 0.6.22 нет OS/filesystem/network sandbox. Поэтому:

- `permissions.network`, `permissions.browser` и `permissions.local_services` являются проверяемыми декларациями, но не firewall;
- отдельная `.venv` изолирует зависимости, но не полномочия процесса;
- checksum внутри неподписанного архива подтверждает целостность архива относительно самого себя, но не личность издателя;
- redaction — defense-in-depth, а не DLP и не разрешение сначала записать секрет;
- Hub не доказывает, что заявленный `risk` соответствует реальному коду;
- Hub не может откатить уже принятый HTTP-запрос, browser click, ордер или blockchain transaction.

Автор ОБЯЗАН считать плагин и все зависимости частью доверенной вычислительной базы. Владелец Hub должен устанавливать пакет только из проверенного источника.

## 4. Обязательная структура исходного пакета и архива

### 4.1. Каноническая структура

Новый софт ОБЯЗАН собираться из каталога следующего вида:

```text
my-soft/
├── hub.plugin.json
├── requirements.txt          # только если есть сторонние зависимости
├── assets/
│   ├── icon.png
│   └── image.webp
└── plugin/
    ├── __init__.py
    ├── main.py
    └── ...
```

После штатной сборки корень ZIP ОБЯЗАН иметь следующий вид, без внешней директории `my-soft/`:

```text
hub.plugin.json
hub.checksums.json
[requirements.txt]
assets/icon.png
assets/image.webp
plugin/__init__.py
plugin/main.py
plugin/...
```

Обязательные элементы:

- `hub.plugin.json` — UTF-8 JSON без комментариев;
- `hub.checksums.json` — генерируется только штатным builder;
- `assets/icon.<ext>` — непустая raster-иконка;
- `assets/image.<ext>` — непустая raster-обложка;
- `plugin/__init__.py`;
- Python-модуль, указанный в `runtime.entrypoint`.

Допустимые необязательные элементы:

- дополнительные Python-модули внутри `plugin/`;
- файл зависимостей, указанный в `runtime.requirements` (обычно `requirements.txt`); если поле не объявлено, отдельная `.venv` не создаётся;
- статические ABI, шаблоны и публичные справочники внутри `data/`;
- `README.md`, `LICENSE`, `NOTICE` в корне.

Тесты, dev-конфигурация и build scripts СЛЕДУЕТ держать вне каталога, передаваемого builder: builder включает все обычные файлы рекурсивно, кроме явно игнорируемых служебных каталогов.

### 4.2. Presentation assets

Для **каждого нового софта по `/5`** поля `presentation` и оба файла assets ОБЯЗАТЕЛЬНЫ. Они не могут быть `null`, пустой строкой, data URI, URL или SVG.

Текущий валидатор допускает отсутствие `presentation` только для установки legacy-пакетов. Legacy fallback на `name`, `description` и `ui.monogram` **не является разрешением** опускать icon/image в новом пакете.

Нормативный визуальный профиль:

- icon: статический квадратный PNG или WebP, до `2 MiB`; СЛЕДУЕТ использовать `512×512` и не опускаться ниже `256×256`;
- image: статический PNG, JPEG или WebP, до `16 MiB`; СЛЕДУЕТ использовать пропорции `16:9` и размер около `1600×900`;
- оба файла ОБЯЗАНЫ иметь реальный raster payload, соответствующий расширению;
- прозрачность допустима у icon; image ОБЯЗАНА иметь осмысленный фон;
- важный текст, номера кошельков, секреты и QR-коды на изображениях ЗАПРЕЩЕНЫ;
- EXIF/XMP и иные необязательные metadata ОБЯЗАНЫ быть удалены; в assets не должно быть GPS, локальных путей, account labels или иных пользовательских данных;
- критичная информация не должна находиться у краёв image, поскольку UI может кадрировать её через `object-fit: cover`;
- анимированные GIF/AVIF, ICO и иные форматы, хотя часть из них принимается legacy-валидатором, для нового софта ЗАПРЕЩЕНЫ.

Пути ОБЯЗАНЫ начинаться с `assets/`, быть относительными POSIX paths, не содержать скрытых сегментов, `..`, backslash и внешних URL.

Автоматически сейчас проверяются путь, расширение, сигнатура payload, наличие файла и byte limit. Installer всё ещё принимает GIF/AVIF/ICO ради legacy admission и не декодирует полное изображение. Геометрия, frame count, pixel count, отсутствие metadata, статичность и качество контента являются обязанностью автора и acceptance review; успешная установка сама по себе не доказывает качество assets нового `/5` пакета.

### 4.3. Запрещённое содержимое

В исходном каталоге и архиве ЗАПРЕЩЕНЫ:

- private keys, seed phrases, mnemonic, пароли, API keys, proxy credentials и реальные аккаунты;
- `.env`, HAR, cookies, session storage, browser profiles, screenshots реальных сессий;
- CSV/XLSX/TXT/JSON с расходниками;
- локальные БД, key stores, certificates с private key;
- `.venv`, `node_modules`, `.git`, кэши, build/release каталоги;
- изменяемое production-состояние;
- symlink, special files, абсолютные пути и платформенные device names;
- исполняемый self-updater или код, подменяющий файлы установленного пакета.

Штатный builder и installer блокируют многие известные имена и private-key payloads, но автор ОБЯЗАН провести отдельный secret scan. Переименование `secrets.json` в нейтральное имя не делает payload допустимым.

### 4.4. Ограничения архива

Архив ОБЯЗАН укладываться в фактические лимиты Hub:

- не более `256 MiB` в сжатом виде;
- не более `512 MiB` после распаковки;
- не более `4000` ZIP entries;
- `hub.plugin.json` и `hub.checksums.json` — не более `2 MiB` каждый;
- файл больше `1 MiB` не может иметь подозрительное compression ratio свыше `100:1`.

Пути ОБЯЗАНЫ быть NFC-normalized, canonical, уникальными также без учёта регистра и переносимыми между поддерживаемыми ОС.

## 5. `hub.plugin.json`: обязательный контракт

### 5.1. Верхний уровень

Для нового софта по `/5` ОБЯЗАТЕЛЬНЫ поля:

- `schema_version` — строго `1`;
- `contract_version` — `SH-SOFTWARE-0.6/5`;
- `id` — постоянный ID модуля;
- `name` — короткое техническое имя;
- `version` — SemVer;
- `description` — короткое описание до 500 символов;
- `presentation`;
- `compatibility`;
- `runtime`;
- `permissions`;
- `actions`.

`author`, `catalog` и `ui` (`accent`, `monogram`) для `/5` необязательны. `ui` не заменяет `presentation`; указанная подпись автора не является криптографическим подтверждением издателя.

`$schema` СЛЕДУЕТ указывать для редактора, но runtime не загружает её по сети.

Неизвестные поля ЗАПРЕЩЕНЫ.

Правила идентичности:

- `id` соответствует `^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$`, максимум 96 символов;
- `id` никогда не меняется между версиями одного продукта;
- `version` имеет вид `MAJOR.MINOR.PATCH` с допустимым prerelease и без build metadata;
- любое изменение кода, assets, manifest или requirements требует увеличения версии;
- уже опубликованный asset под существующей версией ЗАПРЕЩЕНО заменять.

### 5.2. Presentation

Обязательный объект:

```json
{
"presentation": {
  "display_name": "Browser Rewards",
  "description": "Полное описание назначения, ограничений, результата и ожидаемых внешних эффектов.",
  "assets": {
    "icon": "assets/icon.png",
    "image": "assets/image.webp"
  }
}
}
```

Правила:

- `display_name`: 1–120 символов, пользовательское название без версии;
- `description`: 1–4000 символов, полное пользовательское объяснение;
- `assets` содержит ровно `icon` и `image`;
- короткое `description` верхнего уровня и полное `presentation.description` не должны противоречить друг другу;
- маркетинговые обещания, которые код не гарантирует, ЗАПРЕЩЕНЫ.

Весь отображаемый текст пакета — часть интерфейса Hub, а не техническая документация. Для русскоязычного продукта `name`, `description`, `presentation`, названия и описания actions, option `title`/`description`/`placeholder`/`enum_labels`, account messages и безопасные ошибки ОБЯЗАНЫ быть написаны живым понятным русским языком:

- активный залог и короткие предложения; сначала — что произойдёт, затем — что нужно от пользователя;
- обычные слова «софт», «аккаунт», «запуск», «настройка», «результат» вместо видимых `module`, `action`, `payload`, `lifecycle`, `scope`, `permission`, `lease`, `venv` и иных внутренних терминов;
- без англоязычных заглушек и верхнерегистровых техно-ярлыков, если это не имя продукта, общепринятая аббревиатура или точная security confirmation phrase;
- без канцелярита, безличных формулировок, псевдомаркетинга и обещаний результата, которого код ещё не подтвердил;
- одна подсказка отвечает на один вопрос и по возможности укладывается в 1–2 коротких предложения.

Hub показывает manifest-текст без смыслового переписывания. Поэтому неудачная формулировка в пакете считается дефектом самого софта и блокирует UX-приёмку.

### 5.3. Catalog и рабочие разделы

Для `/5` catalog НЕОБЯЗАТЕЛЕН. Если софт должен появиться в отдельном workspace, он объявляет:

```json
{
  "catalog": {
    "sections": ["nft", "testnet"]
  }
}
```

`catalog` ОБЯЗАН быть объектом ровно с одним полем `sections`. `sections` ОБЯЗАН быть непустым unique-массивом только из значений:

- `general` — обычная автоматизация;
- `nft` — вайтлисты, проверки коллекций, минты и другие NFT-сценарии;
- `testnet` — действия тестовых проектов и сетей.

`general` ЗАПРЕЩЕНО объединять с любым другим section. Единственная допустимая multi-section комбинация — `nft + testnet`. Порядок элементов не задаёт приоритет.

Catalog является **только классификацией представления**. Он не выдаёт secret, не расширяет permission, не создаёт network/OS sandbox, не меняет leases, risk class или batch policy и не подтверждает фактическую сеть. Реальное поведение по-прежнему ОБЯЗАНО точно отражаться в `actions[].risk`, `permissions.financial_risk`, `permissions.chains`, resources и коде.

Строгий legacy-контракт `/4` дополнительно применяет project-placement правила:

1. Любой action `testnet_write` ОБЯЗАН иметь section `testnet`.
2. Section `testnet` ЗАПРЕЩЁН, если пакет содержит `mainnet_write` либо `permissions.financial_risk` равен `mainnet`.
3. Гибридный mainnet + testnet продукт в `/4` разделяется на два постоянных plugin ID и два пакета.
4. `read` и `external_write` МОГУТ находиться в `testnet`, если их предметная область — тестовый проект.
5. Section `nft` не задаёт риск: проверка eligibility, отправка WL-анкеты, testnet mint и mainnet mint/list/order — разные actions с разными risks.

Core хранит исходный manifest неизменным и отдаёт вычисленное поле `catalog_sections` отдельно. При admission effective classification ОБЯЗАНА быть записана immutable snapshot в `runs.catalog_sections_json`; история и отчёты используют snapshot, поэтому update/uninstall не меняет раздел старого запуска.

`/5`, `/2`, `/3` и другие manifest без catalog используют fallback: если максимальный риск testnet либо существует `testnet_write`, effective section равен `testnet`; иначе — `general`. NFT ЗАПРЕЩЕНО выводить из `id`, name, description, assets, network hosts или chain IDs.

В `/5` catalog не связан с risk. Это осознанная свобода presentation: один plugin ID МОЖЕТ содержать actions разных сетей и рисков, а core не навязывает разбиение продукта. Каждый action всё равно ОБЯЗАН независимо объявить честные risk, grants, resources и chains; placement не ослабляет эти проверки.

Изменение `catalog.sections` является изменением manifest, требует новой SemVer и явной строки в release notes.

### 5.4. Compatibility и runtime

Новый пакет ОБЯЗАН объявлять:

```json
{
"compatibility": {
  "hub": ">=0.6.22",
  "python": ">=3.12,<3.13",
  "os": ["darwin"]
}
}
```

`compatibility.hub` для `/5` ОБЯЗАН быть не ниже `>=0.6.22`; сейчас поддерживается только форма `>=x.y.z`. Для строгого `/4` исторический минимум остаётся `>=0.6.15`. `compatibility.python` и `compatibility.os` являются обязательной авторской декларацией. Installer проверяет минимальную версию Hub и форму полей, но пока не сопоставляет `os` с текущей системой и не исполняет Python constraint. Поля архитектуры в manifest нет. Поэтому автор ОБЯЗАН указывать только реально протестированные release OS/architecture и выполнить собственный fail-closed runtime check до первого side effect.

Soft Hub 0.6.22 поставляет два автономных desktop target: macOS arm64 и Windows 10/11 x64 с пустым каталогом софтов. Оба содержат core и собственный CPython 3.12 runtime; пользователю НЕ ТРЕБУЮТСЯ системные Python, Node.js, Git или Microsoft Visual C++ Redistributable. Windows bundle включает нужные MSVC runtime DLL, а его Python-зависимости готовятся только из бинарных `cp312-win_amd64` wheels; Hub не компилирует native-зависимости плагина на компьютере пользователя. Packaged Linux target отсутствует.

Наличие desktop target не доказывает переносимость конкретного плагина. Автор ОБЯЗАН указывать в `compatibility.os` только фактически поддержанные системы и выполнить полный smoke **из установленного финального пакета** на каждом заявленном release OS/architecture: `darwin` означает macOS arm64 target, `win32` — Windows 10/11 x64 target. Для Windows все native-зависимости ОБЯЗАНЫ иметь совместимые `cp312-win_amd64` wheels; зависимость, требующая compiler/toolchain либо внешнего Python/Node/VC++ runtime, блокирует приёмку. Копировать `darwin`, `win32`, `linux` в manifest «на всякий случай» ЗАПРЕЩЕНО.

Объект `runtime`:

- `type`: строго `python`;
- `entrypoint`: `package.module:function`;
- `protocol`: строго `soft-hub-jsonl/1`;
- `state_model`: `stateless`, `resumable` или `externally_reconciled`;
- `requirements`: необязательный безопасный относительный путь к файлу зависимостей; для стандартного layout используйте `requirements.txt`;
- `safe_stop`: необязательный boolean; отсутствие означает, что мягкая остановка не обещана;
- `heartbeat_seconds`: необязательный integer `5..300`, default `15`.

`heartbeat_seconds` проходит проверку manifest, но runner 0.6.22 не использует его как watchdog, не убивает зависший процесс и не реализует resume. Плагин ОБЯЗАН самостоятельно ставить bounded timeout на каждую сеть/браузер/дочерний процесс и общий deadline на action/account. Heartbeat показывает активность, но не доказывает прогресс и не заменяет timeout.

`safe_stop: true` разрешено только если плагин регулярно проверяет отмену, прекращает создание новой внешней работы и выполняет bounded cleanup. Это обещание автора, а не автоматически доказанная гарантия.

`state_model: externally_reconciled` — только manifest-декларация архитектуры плагина. Она не является run/account status, не создаёт safety gate и не заставляет Hub запускать проверку. Поле разрешено для пакетов, которые сами имеют durable external truth или опциональное read-only действие проверки.

Hub создаёт для run отдельную OS-containment: на macOS/POSIX это process group, на Windows — Job Object с `KILL_ON_JOB_CLOSE`. Мягкая остановка всегда создаёт cancellation marker, который bootstrap отслеживает на обеих платформах; на POSIX дополнительно идёт `SIGTERM`. Force-stop завершает всю process group через `SIGKILL` либо Windows Job Object; `taskkill /T /F` и kill остаются fallback. Плагин ОБЯЗАН запускать свои дочерние процессы без detach, регулярно вызывать `context.check_cancelled()` между bounded-шагами, прекращать создание новой внешней работы после cancellation и закрывать descendants в bounded cleanup. Cross-platform write-софт ОБЯЗАН быть корректен при force termination, записывать durable business/public operation ID до потери transient context и иметь понятный способ проверить внешний outcome; описывать остановку как гарантированно мягкую ЗАПРЕЩЕНО.

### 5.5. Permissions

Top-level `permissions.secrets` — точное объединение secret kinds всех actions. Допустимы только:

- `evm_private_key`;
- `solana_private_key`;
- `proxy`;
- `email`;
- `email_password`;
- `twitter`;
- `adspower_profile`;
- `capsolver_api_key`;
- `adspower_api_key`.

Legacy secret grants/resources `referral_code` и `referrer_code` в `/4` и `/5` ЗАПРЕЩЕНЫ. Hub не хранит project-specific codes в Vault. `/5` оставляет способ получения project input самому плагину: runtime fetch МОЖЕТ зарегистрировать exact value через неперсистируемый `protect_secret` control-frame, а обычный несекретный user input МОЖЕТ быть bounded option. Данные direct parent при использовании topology выдаются через отдельный `actions[].referral.permissions`, описанный ниже, и входят в top-level union.

Каждый action нового софта ОБЯЗАН иметь собственный `permissions.secrets`. Нельзя пользоваться legacy fallback на top-level права. Каждый action получает минимально необходимый набор, а union наборов всех actions ОБЯЗАН точно совпадать с top-level набором.

Остальные поля:

- `network` — точный список host names без схемы, path, credentials и wildcard;
- `chains` — все положительные chain IDs, на которых возможен chain write;
- `financial_risk` — максимальный риск actions: `none`, `testnet` или `mainnet`;
- `browser` — `true` только если код автоматизирует браузер;
- `local_services` — точный список внешних локальных сервисов.

Если запрошен `adspower_profile` или `adspower_api_key`, manifest ОБЯЗАН содержать:

```json
{
"browser": true,
"local_services": ["adspower"]
}
```

Перечисление host/service не создаёт sandbox. Код ОБЯЗАН самостоятельно запретить неожиданный endpoint и прямое подключение в обход требуемого proxy/profile.

### 5.6. Actions

Каждый элемент `actions` ОБЯЗАН содержать:

- `id` по regex `^[a-z][a-z0-9_-]*$`;
- `name` — понятный глагол + объект, до 80 символов;
- `description` — явное описание внешнего эффекта, до 300 символов;
- `risk`;
- `account_mode`;
- `permissions.secrets`;
- `resources.account` и `resources.settings`;
- `options` — необязательно для `/5`, если действию не нужны параметры.

`output` — необязательная host-owned декларация представления результатов. Для обычного action его можно опустить. Если софт должен дать одну строку статистики на каждый кошелёк, используется точный `output.mode: "account_table"`, описанный в разделе 9.

Новый пакет `/5` с любым `action.output` использует общий минимум `compatibility.hub >=0.6.22`.

Допустимые risks:

| `risk` | Реальное поведение |
|---|---|
| `read` | Только чтение; нет POST/PUT/PATCH/DELETE, подписи, click/submit, изменения browser/profile state. |
| `external_write` | Нефинансовая мутация: регистрация, claim, отправка формы, изменение waitlist/profile, browser click с внешним эффектом. |
| `testnet_write` | Любая подпись или отправка testnet transaction. |
| `mainnet_write` | Любая mainnet transaction, off-chain order/signature с финансовым эффектом или управление активами. |

Любой write ОБЯЗАН использовать `account_mode: one_or_more`. Новый софт НЕ ДОЛЖЕН объявлять `confirmation_phrase` или рисовать собственные подтверждения: нажатие основной кнопки сразу запускает действие. Старое `confirmation_phrase` принимается только для совместимости mainnet-патчей и игнорируется Hub.

Если browser automation только открывает страницу и читает DOM, action МОЖЕТ быть `read`. Если он нажимает кнопку, логинится с созданием сессии, отправляет форму, делает claim или меняет внешний профиль, он ОБЯЗАН быть как минимум `external_write`.

Для NFT/WL workflow ОБЯЗАТЕЛЬНО разделять действия по внешнему эффекту:

| Сценарий | Обязательный risk |
|---|---|
| Проверка eligibility, статуса вайтлиста, коллекции или баланса без логина и мутации | `read` |
| Отправка WL-анкеты, регистрация, подписка, создание сессии или browser submit | `external_write` |
| Минт или иная транзакция в тестовой сети | `testnet_write` |
| Mainnet mint, approval, listing, sale, transfer или marketplace/order signature | `mainnet_write` |

Off-chain order OpenSea с финансовой подписью НЕ является чтением. Минт, листинг и продажа СЛЕДУЕТ делать отдельными actions с отдельными названиями, описаниями и idempotency/external-verification plan; Hub не требует confirmation phrase. Плагин сам отвечает за явно описанную стратегию, bounds и проверку внешнего эффекта.

До любой NFT mainnet подписи код ОБЯЗАН fail-closed проверить официальный domain, фактический chain ID, contract, selector/calldata, recipient, `value`, количество, максимальные gas/fee bounds, token/spender/approval scope и точный marketplace order intent. Trusted addresses и security policy ЗАПРЕЩЕНО принимать как свободный option; они закрепляются в проверяемом коде/данных пакета либо выбираются из закрытого audited enum.

### 5.7. Resources и немедленный preflight

`actions[].resources` — пользовательская декларация того, что должно быть настроено **до создания subprocess**. `permissions.secrets` отвечает за выдачу значения коду; `resources` отвечает за понятное предупреждение пользователю. Для нового софта ОБЯЗАТЕЛЬНЫ оба механизма.

Допустимые account resources:

| `resources.account` | Соответствующее secret permission | Значение |
|---|---|---|
| `private_key` | `evm_private_key` | EVM private key выбранного аккаунта. |
| `solana_private_key` | `solana_private_key` | Проверенный полный 64-byte Solana Ed25519 keypair выбранного аккаунта в canonical base58. |
| `proxy` | `proxy` | HTTP proxy выбранного аккаунта. |
| `email` | `email` | Email выбранного аккаунта. |
| `email_password` | `email_password` | Пароль email выбранного аккаунта. |
| `twitter` | `twitter` | Twitter/X credential выбранного аккаунта. |
| `adspower_profile` | `adspower_profile` | AdsPower profile ID выбранного аккаунта. |

Допустимые global settings resources:

| `resources.settings` | Соответствующее secret permission | Значение |
|---|---|---|
| `capsolver` | `capsolver_api_key` | Один общий Capsolver API key. |
| `adspower_api` | `adspower_api_key` | Один общий AdsPower API key. |

Глобальные Capsolver/AdsPower API keys должны содержать минимум 4 символа: более короткое значение Hub отклоняет, потому что exact-value redactor намеренно не заменяет строки короче четырёх символов.

Пользователь вводит оба общих ключа только в разделе Hub **Аккаунты**. Софт НЕ ДОЛЖЕН создавать для них собственное поле в `actions[].options`, просить ключ через `input()` или хранить копию в файлах. Имена `capsolver`, `capsolver_key`, `capsolver_api_key`, `cap_solver_api_key`, `captcha_key`, `captcha_api_key`, `adspower_api`, `adspower_key` и `adspower_api_key` зарезервированы и отклоняются валидатором options. Софту остаётся только объявить соответствующие permission и `resources.settings`.

Инварианты нового софта:

1. `resources` содержит ровно `account` и `settings`, даже если один список пуст.
2. Каждый resource имеет соответствующий permission выбранного action.
3. Каждый permission из таблиц имеет соответствующий resource. Для `email` и `email_password` используются два независимых resource kinds; требовать один из них «заодно» без фактического чтения ЗАПРЕЩЕНО.
4. Action, которому нужны и адрес, и пароль email, ОБЯЗАН объявить оба permissions и оба account resources.
5. Action с account resources ОБЯЗАН иметь `account_mode: one_or_more`.
6. Global settings выдаются отдельно через `context.settings`; они никогда не являются account resource. Action только с global settings МОЖЕТ иметь `account_mode: none`, если его предметная логика не относится к аккаунтам и risk допускает такой режим.
7. Новый код ОБЯЗАН читать Capsolver как `context.settings.secret("capsolver")`, а AdsPower API key как `context.settings.secret("adspower_api")`.
8. Секреты ЗАПРЕЩЕНО запрашивать через `options` или `input()`.

Host-owned реферальная модель — это только зашифрованная топология `child → direct parent`; project codes не являются Vault resources и не входят в topology API. Строгий `/4` дополнительно требует project-runtime workflow и запрещает manual code option. Свободный `/5` не навязывает это бизнес-правило: плагин МОЖЕТ получить code во время run либо принять bounded option, если так устроен проект. В обоих случаях code ЗАПРЕЩЕНО выводить в events/results/logs/files; runtime secret СЛЕДУЕТ немедленно передать в `protect_secret` для defense-in-depth redaction.

Hub 0.6.22 показывает эту топологию как графический rooted forest: каждый root располагается сверху своей ветки, descendants — по уровням ниже, а направленные линии явно соединяют parent с direct children. Карту можно перетаскивать, масштабировать вокруг курсора, вписывать целиком и открывать нужную ветку через мини-карту; клавиатура поддерживает стрелки, `Shift + стрелки`, `+`, `-`, `0` и `Home`. Рабочая область вычисляет доступную высоту окна и не обрезает canvas снизу. Поиск, выбор узла, путь до root и смена одного direct parent являются только редактором topology; pan/zoom viewport, сжатая таблица, свободное позиционирование и project-specific codes не являются persistence-контрактом. В persistence Hub остаются только account identity и nullable `referrer_account_id`; viewport, координаты, layout и project-specific codes не сохраняются.

Если `/5` или `/4` action использует host topology, он ОБЯЗАН иметь `account_mode: "one_or_more"` и объект следующей точной формы:

```json
{
"referral": {
  "mode": "project_runtime",
  "parent_required": true,
  "parent_access": "shared_read",
  "permissions": {
    "secrets": ["evm_private_key", "proxy"]
  },
  "resources": {
    "account": ["private_key", "proxy"]
  }
}
}
```

- `parent_required: true` блокирует запуск, если хотя бы у одного выбранного child нет parent; `false` разрешает root;
- `parent_access: "shared_read"` допустим, только если доступ к parent не меняет его внешнее состояние; любой login/session/code rotation/write требует `exclusive`;
- parent `permissions.secrets` и `resources.account` совпадают точно по обычной account-resource таблице; глобальные settings и legacy referral resources здесь запрещены;
- `adspower_profile` у parent всегда требует `parent_access: "exclusive"`.

Runner фиксирует revision графа при admission, выдаёт только уникальных direct parents выбранных targets и только указанные parent secrets. Targets и parents закрепляются `run_account_pins` до terminal run; `exclusive` дополнительно получает service-lease. Плагин берёт parent только из `context.referrals.parent_for(child.id)` или ограниченного набора `context.referrals.parents`, получает код у API конкретного проекта, кэширует его только в памяти run и сам подставляет child.

`POST /api/accounts/referral-topology` принимает только `expected_revision` и полный список `relationships`; каждый текущий account ОБЯЗАН встречаться ровно один раз:

```json
{
  "expected_revision": "<64 lowercase hex>",
  "relationships": [
    {"child_account_id": "<canonical UUID>", "parent_account_id": null},
    {"child_account_id": "<canonical UUID>", "parent_account_id": "<canonical UUID>"}
  ]
}
```

Backend CAS отклоняет stale revision, duplicate/unknown/missing ID, self-link и цикл до перешифрования. Re-import сохраняет parent-связь, удаление parent отсоединяет прямых children, plaintext export топологию не включает.

Host 0.6.22 выполняет первичный admission-preflight до постановки run в очередь: проверяет состояние Vault, выбранные аккаунты и non-secret configured-флаги каждого объявленного account/global resource, а UI сразу показывает конкретный недостающий тип. При существующем закрытом Vault одиночный start и batch start возвращают `423 Locked` до создания или replay-проекции run. После получения execution slot runner повторно расшифровывает и проверяет обязательные значения до создания subprocess. Независимо от обоих host checks entrypoint ОБЯЗАН повторить fail-closed проверку выданных значений до первого network/browser/write side effect.

`actions[].resources` и `presentation` обязательны для `/5` и `/4`; отсутствие допускается только у прежних legacy-пакетов.

### 5.8. Options

В `/5` `actions[].options` НЕОБЯЗАТЕЛЕН, если действию не нужны параметры. Если options объявлены, они используют закрытый плоский subset JSON Schema. Строгий `/4` дополнительно требует объект даже без параметров и ровно четыре корневых поля:

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

Нормативные правила:

- option key ОБЯЗАН быть `snake_case` по `^[a-z][a-z0-9_]{0,63}$`; всего допускается не более 40 полей;
- допустимы только primitive types `boolean`, `string`, `integer`, `number`; arrays, nested objects, `null`, `$ref`, conditional schema и произвольные дополнительные keywords ЗАПРЕЩЕНЫ;
- `/5` МОЖЕТ опустить UI-подсказки option-поля: default type — `string`, title — option key, description — нейтральная подсказка, group — «Основные настройки», order — стабильная сортировка; `/4` требует `type`, `title`, `description` и `x-ui`;
- `enum` допускается только как непустой уникальный список строк; `/5` может показать сами значения без `x-ui.enum_labels`, но понятные labels СЛЕДУЕТ объявить явно;
- string bounds не превышают 16 000; `/5` МОЖЕТ использовать `pattern`, но entrypoint всё равно повторно проверяет предметный формат;
- `integer` и `number` МОГУТ объявить конечные `minimum`/`maximum`; положительный `multipleOf` задаётся, когда нужен фиксированный шаг;
- безопасный `default` СЛЕДУЕТ задавать, когда он существует; required-поле МОЖЕТ не иметь default, если нужен явный выбор;
- неизвестные option values, missing required, типы, enum, numeric bounds/step и string lengths backend проверяет до доступа к Vault; entrypoint повторяет предметную проверку до side effect;
- secrets, адреса доверенных contracts/RPC, chain ID для подписи и иные security-critical policy values через options ЗАПРЕЩЕНЫ.

Option-copy говорит с пользователем, а не повторяет schema: `title` коротко называет выбор, `description` объясняет его эффект и безопасный ориентир, `placeholder` показывает только несекретный формат, `enum_labels` читаются как нормальные варианты ответа. Текст вида «введите integer», «укажите payload», «выберите action scope» или голая техническая константа без объяснения ЗАПРЕЩЕНЫ.

`x-ui` — необязательная в `/5` и обязательная в строгом `/4` UI-метаинформация option-поля:

| Поле | Правило |
|---|---|
| `group` | Короткое понятное имя смысловой группы, обязательно. |
| `order` | Integer `0..1000`, стабильно задающий порядок внутри формы; значение ОБЯЗАНО быть уникальным среди всех options action. |
| `control` | `input`, `textarea`, `slider` или `dual_range`; `textarea` допустим только для свободной строки, slider-контролы — только для `integer`/`number`. |
| `placeholder` | Не пример секрета и не замена description; максимум 160 символов. |
| `unit` | Короткая единица измерения до 24 символов, например `сек` или `попыток`; разрешена только `integer`/`number`. |
| `advanced` | `true` убирает редко меняемый параметр в раскрываемую группу дополнительных настроек. Одна `group` не может смешивать основные и advanced-поля. |
| `enum_labels` | Обязательный для enum полный mapping каждого string value в непустую понятную подпись до 120 символов. |
| `range` | Только для `dual_range`: строгий объект `{"id": "snake_case", "role": "from"|"to"}`. |

Bounded numeric Hub по умолчанию показывает ползунком с отдельным точным вводом без native spinner; `control: input` оставляет ручной ввод, `control: slider` делает ползунок явным требованием. Явный slider ОБЯЗАН иметь безопасный `default`, `minimum < maximum` и сетку от 1 до 1000 шагов. Для `number` ОБЯЗАТЕЛЕН положительный `multipleOf`; `minimum`, `maximum` и `default` лежат на его сетке. Integer slider использует `multipleOf`, либо шаг `1`, и не выходит за безопасные integer-границы JavaScript.

`dual_range` ОБЯЗАН состоять ровно из двух отдельных primitive options с общим `range.id` и ролями `from`/`to`. У пары совпадают `type`, `title`, `description`, numeric bounds, `multipleOf`, `group`, `unit` и `advanced`; `order` остаются разными. Оба поля одинаково required/optional, оба имеют defaults, причём `from <= to`. Пара занимает одну UI-карточку и считается одним primary-control. Backend до создания run отклоняет неполную или перевёрнутую пару независимо от renderer.

Строгий `/4` запрещает более 7 primary-параметров. `/5` не навязывает проекту это число, но длинную форму СЛЕДУЕТ группировать или делить на понятные actions. Если объявлены `title` и `description`, они отвечают, что изменится, в каких единицах и какой внешний риск связан с выбором.

Options существуют только для **одного запуска**: Hub не обещает сохранять их как постоянные настройки софта. Одиночный и пакетный запуск строят одну и ту же полную schema-форму. В batch первым показывается первый action из manifest, поэтому основной пользовательский сценарий СЛЕДУЕТ ставить первым. Любой action и его options остаются доступными для просмотра при незаполненном resource; resource preflight блокирует только submit. Каждое действие имеет независимый draft: пользователь видит и меняет его sliders, dual ranges, enum, boolean, string и required-поля до общего старта; submit передаёт exact options именно этого элемента. Смена action сбрасывает/перестраивает и раскрывает только его карточку. Manifest defaults задают начальное состояние, но не подменяют скрытой автоматикой обязательный выбор пользователя и не наследуют значения прошлого run.

`account_concurrency` — host option для account workers. В `/5` она НЕОБЯЗАТЕЛЬНА: без неё Hub использует safe effective default `1`. Объявляйте её, только если софт действительно поддерживает параллельную обработку аккаунтов. Строгий `/4` требует поле у каждого `one_or_more`; при `account_mode: "none"` оно ЗАПРЕЩЕНО:

```json
{
"account_concurrency": {
  "type": "integer",
  "title": "Параллельные аккаунты",
  "description": "Сколько профилей софт обрабатывает одновременно.",
  "default": 3,
  "minimum": 1,
  "maximum": 5,
  "multipleOf": 1,
  "x-ui": {
    "group": "Выполнение",
    "order": 0,
    "unit": "аккаунтов"
  }
}
}
```

Инварианты:

- `minimum` ровно `1`, `multipleOf` ровно `1`, `default` — safe integer внутри объявленного диапазона;
- `maximum` не выше `20` для HTTP/API action и `5`, если top-level `permissions.browser` равен `true`;
- `account_concurrency` НЕ входит в `required`: omission в request всегда должен давать безопасный manifest `default`;
- `/4` требует `x-ui.group` ровно `Выполнение`; `/5` может использовать стандартное оформление поля Hub;
- safe default выбирается по риску API/provider: обычно `5` для read-only HTTP, `3` для HTTP write и `1..3` для browser; это рекомендации, не принудительные универсальные числа;
- при admission Hub применяет `effective = min(requested, selected_account_count)`, сохраняет его в run и передаёт одинаково как `context.account_concurrency` и `context.options["account_concurrency"]`.

Это ограничение workers **внутри одного plugin subprocess**. Оно не равно batch `execution_mode`: `parallel|sequential` определяет порядок разных софтов, а `account_concurrency` — профили внутри одного софта.

## 6. Минимальный манифест нового browser-софта

Следующий пример соответствует рекомендуемому `/5`. Он сознательно показывает optional catalog, UI hints и `account_concurrency`; их наличие объясняется возможностями этого софта, а не универсальным рецептом Hub.

```json
{
  "$schema": "https://soft-hub.local/schemas/plugin-v1.json",
  "schema_version": 1,
  "contract_version": "SH-SOFTWARE-0.6/5",
  "id": "io.sprintray.browser-rewards",
  "name": "Browser Rewards",
  "version": "1.0.0",
  "description": "Автоматизирует получение browser-награды через отдельные профили AdsPower.",
  "author": "sprintray",
  "presentation": {
    "display_name": "Browser Rewards",
    "description": "Открывает привязанный к каждому аккаунту профиль AdsPower, проверяет готовность ресурсов и выполняет один заявленный сценарий. Результат и статус отображаются отдельно по каждому аккаунту.",
    "assets": {
      "icon": "assets/icon.png",
      "image": "assets/image.webp"
    }
  },
  "catalog": {
    "sections": ["general"]
  },
  "compatibility": {
    "hub": ">=0.6.22",
    "python": ">=3.12,<3.13",
    "os": ["darwin"]
  },
  "runtime": {
    "type": "python",
    "entrypoint": "plugin.main:run",
    "protocol": "soft-hub-jsonl/1",
    "state_model": "externally_reconciled",
    "requirements": "requirements.txt",
    "safe_stop": true,
    "heartbeat_seconds": 15
  },
  "permissions": {
    "secrets": [
      "email",
      "adspower_profile",
      "capsolver_api_key",
      "adspower_api_key"
    ],
    "network": [
      "local.adspower.net",
      "api.capsolver.com",
      "rewards.example.com"
    ],
    "chains": [],
    "financial_risk": "none",
    "browser": true,
    "local_services": ["adspower"]
  },
  "actions": [
    {
      "id": "claim_reward",
      "name": "Получить награду",
      "description": "Открывает AdsPower-профиль и отправляет одну внешнюю заявку на награду.",
      "risk": "external_write",
      "account_mode": "one_or_more",
      "permissions": {
        "secrets": [
          "email",
          "adspower_profile",
          "capsolver_api_key",
          "adspower_api_key"
        ]
      },
      "resources": {
        "account": ["email", "adspower_profile"],
        "settings": ["capsolver", "adspower_api"]
      },
      "options": {
        "type": "object",
        "properties": {
          "account_concurrency": {
            "type": "integer",
            "title": "Параллельные аккаунты",
            "description": "Сколько AdsPower-профилей софт обрабатывает одновременно.",
            "default": 3,
            "minimum": 1,
            "maximum": 5,
            "multipleOf": 1,
            "x-ui": {
              "group": "Выполнение",
              "order": 0,
              "unit": "аккаунтов"
            }
          },
          "max_attempts": {
            "type": "integer",
            "title": "Максимум попыток",
            "description": "Не более трёх попыток на аккаунт; безопасное значение — 1.",
            "minimum": 1,
            "maximum": 3,
            "default": 1,
            "x-ui": {
              "group": "Выполнение",
              "order": 10,
              "unit": "попыток",
              "advanced": false
            }
          }
        },
        "required": [],
        "additionalProperties": false
      }
    }
  ],
  "ui": {
    "accent": "#B9654B",
    "monogram": "BR"
  }
}
```

## 7. Python entrypoint и JSONL-протокол

### 7.1. Форма entrypoint

Entrypoint ОБЯЗАН быть sync- или async-функцией с одним аргументом `HubContext`:

```python
from soft_hub.sdk import HubContext


def run(context: HubContext) -> dict:
    ...
```

Он ОБЯЗАН:

- проверить `context.action_id` по явному allowlist;
- повторно проверить types/ranges всех options;
- использовать только `context.accounts`, выданные выбранному run;
- возвращать небольшой JSON-serializable `dict` с агрегатной summary;
- завершаться без `input()`, GUI prompt и чтения дополнительных строк stdin.

Если entrypoint возвращает не `dict`, Hub сохранит пустую summary. Несериализуемый объект приводит к failed run.

### 7.2. Правила stdout/stderr

Stdout зарезервирован под JSONL frames `soft-hub-jsonl/1`. Автор ЗАПРЕЩЕНО:

- печатать в stdout;
- заменять protocol emitter;
- самостоятельно формировать terminal frames;
- запускать дочерний процесс, наследующий protocol stdout;
- смешивать progress bar, Rich, tqdm или logging console handler с stdout.

Bootstrap после decode context оборачивает plugin stderr (включая `.buffer`) локальным `context.sanitize_text`, а после импорта перенаправляет туда обычный `print()`. Это уменьшает race, когда runtime secret уже зарегистрирован через `protect_secret`, но случайный вывод успевает пересечь process boundary до host-redaction. Это аварийная защита, а не API логирования: stderr сохраняется как warning и может попасть в скачиваемый журнал; вывод до регистрации, split/custom encoding, файл или сеть не гарантированно закрыты.

Для телеметрии ОБЯЗАТЕЛЬНО использовать:

- `context.log(...)`;
- `context.progress(...)`;
- `context.account_state(...)`;
- `context.result(...)`.

Три malformed protocol frames приводят к принудительному завершению. Одна строка ограничена `65 536` декодированными Unicode-символами; её UTF-8-представление может быть больше `64 KiB`. Весь run ограничен `50 000` строками. Софт ОБЯЗАН агрегировать повторяющиеся события и не логировать каждый network poll.

### 7.3. HubContext и секреты

Публичные поля `HubAccount`: `id`, `label`, `evm_address`; для referral plan добавлены safe topology metadata `referrer_account_id` и `referral_depth`. `referral_depth` — относительная глубина среди selected targets текущего plan, а не абсолютная глубина полного Vault-графа. По нормативному контракту нового софта account secret ОБЯЗАН читаться только через:

```python
value = account.secret("adspower_profile")
```

Global setting читается отдельно через `HubSettings`:

```python
adspower_api_key = context.settings.secret("adspower_api")
capsolver_api_key = context.settings.secret("capsolver")
```

Имена в `context.settings.secret()` совпадают с `actions[].resources.settings`, а не с внутренними permission names. Если permission не выдан или значение отсутствует, `secret()` поднимает `KeyError`. Хотя `HubAccount` и `HubSettings` технически реализуют `Mapping` для legacy-совместимости, доступ к секретам через `[]`, iteration и `dict(...)` для нового софта ЗАПРЕЩЁН. Нельзя сериализовать эти объекты или передавать их целиком сторонней библиотеке. Их `repr` скрыт только как дополнительная защита.

`context.account_concurrency` содержит уже зажатое effective-число workers. Канонический путь для независимых targets:

```python
def process_account(account):
    context.check_cancelled()
    # один client/session и mutable state только для этого account
    ...
    context.check_cancelled()
    return public_result

results = context.map_accounts(process_account)
```

`map_accounts()` использует не более `min(context.account_concurrency, len(accounts))` threads и возвращает tuple в исходном account-order, хотя events между workers могут прийти в другом порядке. Ожидаемую per-account ошибку ОБЯЗАН обработать сам worker и завершить его lifecycle. Unhandled exception отменяет ещё не стартовавшие futures, дожидается уже работающих и повторно поднимает первую ошибку. Каждый worker ОБЯЗАН иметь finite network/browser timeouts, вызывать `check_cancelled()` до и между external side effects, не создавать detached work и завершать cleanup до возврата.

Для `action.referral.mode: "project_runtime"` топология доступна только через:

```python
for level in context.referral_levels:
    # Уровни идут parent-first; аккаунты одного уровня можно обработать bounded-параллельно.
    for child in level:
        parent = context.referrals.parent_for(child.id)
        if parent is None:
            continue
        code = fetch_project_code(parent)
        code = context.protect_secret(code)
        apply_project_code(child, code)
```

`context.referrals.parent_for(child.id)` — единственный источник direct parent для child; `context.referrals.parents` возвращает bounded exact-grant набор. Назначать parent по label, address, порядку в `context.accounts` или собственному mapping ЗАПРЕЩЕНО. `context.referral_levels` группирует выбранные targets parent-first; разрешается parallelism только внутри уровня, если следующий уровень зависит от кодов предыдущего.

Каждый полученный у project API referral/invite code ОБЯЗАН немедленно пройти `context.protect_secret(code)` **до** любых `log/result/exception/print`. SDK отправляет exact code в отдельном control-frame в host process только для in-memory Redactor; frame не записывается в events/results/log. Exact value кратковременно находится в памяти plugin/host текущего run. Bootstrap локально санитизирует последующие text/binary stderr writes, но это только defense-in-depth: raw `print`, traceback, exception interpolation и вывод code в SDK event всё равно ЗАПРЕЩЕНЫ, а split/custom encoding/file/network могут обойти защиту. Код не входит во входной Vault/run context/options, persisted events/results/summary/log, scratch или файлы; после применения кэш ОБЯЗАН быть очищен best effort.

`plugin_root` предназначен для чтения immutable payload. Запись туда ЗАПРЕЩЕНА. `scratch_dir` уникален для run и одновременно является cwd, но в текущей реализации может сохраниться после завершения. Поэтому secrets, cookies, HAR, screenshots, browser profiles и raw responses в scratch ЗАПРЕЩЕНЫ. Любой допустимый временный файл ОБЯЗАН иметь случайное имя без account/secret values и удаляться в bounded `finally`.

## 8. Per-account lifecycle и прогресс

### 8.1. Обязательная последовательность

Для action с `account_mode: one_or_more` Hub создаёт строки аккаунтов в состоянии `queued`. Для каждого аккаунта, предметная обработка которого началась, плагин ОБЯЗАН отправить:

1. один `account_state(status="running", ...)` перед предметной работой;
2. ноль или больше промежуточных `log/progress`;
3. ровно один terminal `account_state`.

Допустимые terminal statuses:

| Status | Когда использовать |
|---|---|
| `succeeded` | Все обязательные шаги аккаунта подтверждены. Progress становится 1. |
| `partial` | Часть независимых шагов подтверждена, часть завершилась ошибкой; message/result явно объясняет подтверждённую часть и недоказанный итог. |
| `failed` | Получена ошибка, crash или итог внешнего write не доказан. В последнем случае message ОБЯЗАНО предупреждать о проверке внешнего state перед повтором. |
| `skipped` | Аккаунт сознательно не обрабатывался по безопасному правилу. |
| `blocked` | Не выполнен предметный precondition: баланс, доступность сервиса, статус аккаунта и т. п. |
| `cancelled` | Оператор остановил работу. Если write мог уйти наружу, message не обещает rollback и предупреждает о проверке перед повтором. |

После terminal status второе terminal-событие и возврат к `running` ЗАПРЕЩЕНЫ и будут отклонены runner.

Если отмена произошла до начала обработки аккаунта, Hub сам переводит оставшийся `queued` state в `cancelled`. При обычном завершении без terminal account state Hub может показать системный `unknown`; это протокольный дефект, а не статус, который разрешено эмитить новому plugin. Текст `«успешно»` в log/result не меняет lifecycle.

Единственное legacy-исключение core 0.6.2 — точечный bridge для известных first-party Checkpoint/Sekai/Umia `1.0.0`: только их заранее перечисленные actions и ровно один типизированный account-scoped `account_summary` могут восстановить старую проекцию. Этот bridge НЕ является частью контракта нового софта, не применяется к сторонним ID/версиям и не отменяет требование terminal `account_state`.

`reviewed` — не protocol/account status плагина, а опциональная отметка **всего run**, которую создаёт Hub по действию оператора. Review/hide убирает известную terminal run/account-ошибку из живого alert, но сохраняет исходные `error`, events, results и account states. Он идемпотентен, не является проверкой внешнего outcome и никогда не разрешает/не блокирует rerun. Terminal transition уже освободил leases/pins.

Новому плагину ЗАПРЕЩЕНО эмитить `needs_attention`, `reviewed` или `reconciled`, использовать их в `context.result(status=...)` или изображать review как проверку внешнего состояния. Исторические `needs_attention` Hub нормализует в `failed` при startup/финализации и снимает их оставшиеся leases/pins.

### 8.2. Stage, message и progress

- `stage` — стабильный machine ID по `^[a-z][a-z0-9_.-]{0,63}$`;
- stage ЗАПРЕЩЕНО использовать как пользовательское предложение;
- `message` — короткий безопасный текст для пользователя без секретов и внутренних traceback;
- per-account `progress` — конечное JSON-число `0..1`, только монотонно; boolean, строка, NaN/infinity, выход за границы и регресс являются protocol error, Hub их не clamp-ит;
- для `account_mode: one_or_more` ЕДИНСТВЕННЫЙ расчётный источник — `account_state/progress` либо `context.progress(..., account_id=...)`; run-level event без `account_id` сохраняется как telemetry, но не влияет на процент запуска;
- Hub вычисляет run progress как `AVG(account.progress)` по всем выбранным аккаунтам, включая ещё не начатые строки `queued=0`; один быстрый worker поэтому не завышает общий результат;
- global `context.progress` без `account_id` разрешён как расчётный источник только для `account_mode: none`.

Рекомендуемые стабильные stages: `preflight`, `browser_starting`, `browser_attaching`, `auth_check`, `automation`, `submitting`, `confirming`, `cleanup`, `external_outcome_unknown`, `completed`, `cancelled`.

Hub может локализовать известный stage или показать общий текст. Поэтому понятный `message` ОБЯЗАТЕЛЕН на каждом существенном переходе.

До реализации action автор ОБЯЗАН составить конечный progress plan из фактических единиц работы и весов. Нормативная шкала:

| Точка | Требование |
|---|---|
| Вход/preflight | Первое состояние `running`, `0 < progress <= 0.10`. |
| Промежуточная работа | Для workflow дольше 2 секунд либо с 3+ внешними/предметными шагами — минимум три различных milestone в `0.10..0.95`. |
| Повторяемая работа | `base + span * completed_weight / total_weight`; numerator увеличивается только после завершённой единицы, denominator фиксируется до её запуска. |
| Успех | Ровно `1.0` только после подтверждения обязательного результата и cleanup. |
| Известная ошибка/skipped/блокировка/отмена/неоднозначность | Progress не передаётся в terminal `account_state`; если legacy adapter всё же передаст его, Hub проигнорирует значение и сохранит последний подтверждённый процент. |
| Partial | `1.0` допустим только если весь заявленный план обработан и все частичные итоги известны; иначе сохраняется последний milestone. |

NFT/WL action ОБЯЗАН привязать шкалу к реальным предметным этапам, например `preflight → browser/auth → eligibility → form_submitted` для вайтлиста или `preflight → quote/simulation → signed → broadcast → receipt → indexed` для минта. Этапы, которых в конкретном action нет, не симулируются. Ожидание receipt или индексации показывает heartbeat и стабильный stage без таймерного роста процента. `1.0` ЗАПРЕЩЁН до подтверждённого обязательного результата: локальная подпись, рассчитанный tx hash или отправленный browser click сами по себе успех не доказывают.

Milestone разрешено повышать только после проверяемого факта: валидирован preflight, browser действительно attached, HTTP/RPC response получен и проверен, bounded item обработан, receipt подтверждён, journal fsync завершён, cleanup подтверждён. Progress по elapsed time, бесконечная CSS/terminal-анимация, случайное приращение, повышение на каждом retry без bounded retry budget и вывод процента из текста log ЗАПРЕЩЕНЫ.

Если один внешний вызов долго не завершается, автор МОЖЕТ отправлять `heartbeat` или повторять тот же stage/message с тем же progress. Это показывает живость, не придумывая выполненную работу. Для параллельных workers progress одного аккаунта должен сериализоваться; события разных аккаунтов Hub агрегирует независимо.

### 8.3. Параллельность

Каждый action, который объявил `account_concurrency`, ОБЯЗАН фактически использовать `context.account_concurrency`, предпочтительно через `context.map_accounts()`. Показать control в UI, но игнорировать его — нарушение контракта. Последовательный `/5` action без этого option корректно использует effective default `1`. Софт дополнительно ОБЯЗАН:

- не превышать effective host limit и не создавать внутренний неограниченный executor;
- не разделять mutable client/session между аккаунтами;
- не использовать один AdsPower profile одновременно для двух workers;
- сериализовать lifecycle одного аккаунта;
- вызывать `context.check_cancelled()` в начале worker и до/между external side effects;
- дождаться завершения/cleanup всех созданных workers перед возвратом;
- обрабатывать ожидаемую per-account ошибку внутри worker, чтобы один профиль не обрывал всю пачку без корректных terminal states;
- учитывать, что порядок событий между threads недетерминирован.

Runtime emitter сериализует SDK emit-вызовы внутренним lock, а `protect_secret` защищает свой in-process список lock. Это не делает бизнес-логику, HTTP client, browser driver, cache и project SDK thread-safe. Реферальные зависимости обрабатываются по `context.referral_levels`: внутри уровня — bounded parallelism, между зависимыми уровнями — barrier.

## 9. Structured results

`context.result()` используется для долговременного предметного результата, а не для каждого шага.

Нормативные поля:

- `title` — понятный итог до 300 символов;
- `kind` — стабильный machine ID по стилю `snake_case`, максимум 80 символов;
- `status` — один из `succeeded`, `partial`, `failed`, `skipped`, `blocked`;
- `account_id` — ОБЯЗАТЕЛЕН для результата конкретного аккаунта и должен принадлежать run;
- `data` — небольшой JSON object с публичными предметными значениями.

Стабильная семантика results:

- для каждого начатого аккаунта, у которого появился предметный итог, action ОБЯЗАН создать ровно один финальный account-scoped result; дополнительные предметные артефакты разрешены только с отдельными документированными `kind`, а не как копии каждого log;
- аккаунт, оставшийся `queued` и отменённый Hub до предметной обработки, МОЖЕТ не иметь result; action с `account_mode: none`, создающий долговременный предметный итог, ОБЯЗАН создать хотя бы один run-scoped result без `account_id`;
- `kind` — публичный стабильный идентификатор схемы результата. Его значение и смысл существующих keys в `data` нельзя менять внутри совместимой major-версии; добавления должны быть backward-compatible, а breaking change требует нового kind либо major version;
- result `status` ОБЯЗАН совпадать с terminal account outcome по смыслу. Result не управляет lifecycle и никогда не заменяет `account_state`;
- для отмены до появления предметного итога result обычно не создаётся: `cancelled` — terminal account status, но не result status;
- payload ОБЯЗАН быть небольшим и предметным. Bulk-таблицы, raw responses, traceback, DOM/HAR и event history относятся к журналу или внешнему безопасному хранилищу, а не к `data`.

Hub сохраняет results независимо от последующего обновления или удаления модуля, поэтому renderer и экспорт истории должны уметь читать старые `kind/data`. Текущий SDK/runner проверяет JSON-serializability, account membership, size/redaction boundaries и сохраняет переданные `kind/status`, но не может доказать их предметный смысл, соответствие terminal state или backward compatibility. Эти инварианты проверяются кодом плагина и acceptance tests.

Допустимые примеры data: публичный EVM address, chain ID, transaction hash, публичный remote operation ID, числовые counters, boolean flags. Даже публичные идентификаторы СЛЕДУЕТ минимизировать, если они позволяют связать пользователя между сервисами.

Допустимые NFT-поля: стабильный collection ID/slug, публичный whitelist status, mint status, количество, token ID, chain ID, transaction hash и безопасный listing status. Допустимые testnet-поля: сеть, публичные transaction hashes, балансы тестовых токенов, выполненные задания, points/rank и eligibility. Raw signature, raw signed transaction, полный marketplace/OpenSea order, permit payload, cookie, authorization и session material ЗАПРЕЩЕНЫ даже если часть этих данных можно проверить публично.

В results и возвращаемой summary ЗАПРЕЩЕНЫ EVM/Solana private key или seed, proxy, email, email password, Twitter credential, Capsolver/AdsPower keys, AdsPower profile ID, cookies, access tokens, authorization headers, raw HTTP payload, DOM dump, screenshot, путь к secret-bearing файлу и текст исключения сторонней библиотеки.

Return summary ОБЯЗАНА быть агрегатной, например:

```json
{"total": 10, "succeeded": 8, "failed": 1, "cancelled": 1}
```

Summary не заменяет terminal `account_state`.

### 9.1. Parsing и таблица статистики

**Parsing** — это не отдельный runtime, protocol event или особый класс риска. Это обычный action с `risk: "read"` и `account_mode: "one_or_more"`, который читает внешнее или локальное состояние без любой мутации. Логин с созданием сессии, POST/PUT/PATCH/DELETE, claim, browser click/submit, подпись или транзакция больше не являются Parsing и требуют честный write-risk.

Для вывода одной строки на каждый выбранный кошелёк action МОЖЕТ объявить:

```json
{
  "output": {
    "mode": "account_table",
    "title": "Статистика аккаунтов",
    "primary_kind": "account_snapshot",
    "columns": [
      {"key": "points", "title": "Очки", "type": "integer", "aggregate": "sum"},
      {"key": "rank", "title": "Ранг", "type": "integer", "aggregate": "avg"},
      {"key": "eligible", "title": "Допущен", "type": "boolean"}
    ]
  }
}
```

`output` для этого режима имеет ровно четыре поля:

- `mode` — строго `account_table`;
- `title` — живое понятное название таблицы;
- `primary_kind` — стабильный `snake_case` kind главного account-scoped result;
- `columns` — от 1 до 12 уникальных колонок.

Каждая колонка содержит `key`, `title`, `type` и необязательный `aggregate`. `key` указывает только на прямое поле объекта `result.data`; JSONPath, dotted path, вложенные объекты, массивы, raw response и user-defined HTML/template ЗАПРЕЩЕНЫ. Допустимые `type`: `string`, `integer`, `number`, `decimal_string`, `boolean`. `decimal_string` используется для точных больших или дробных значений, которые нельзя без потерь превращать в JavaScript number.

`aggregate` может быть `sum`, `avg`, `min` или `max`, допустим только для `integer`, `number` и `decimal_string`. В одном `output` может быть не более четырёх колонок с `aggregate`.

На каждый начатый кошелёк с появившимся предметным итогом action ОБЯЗАН создать ровно один `context.result(..., kind=primary_kind, account_id=account.id, data={...})`. Его `status` ОБЯЗАН быть одним из `succeeded`, `partial`, `failed`, `skipped`, `blocked` и совпадать по смыслу с terminal `account_state`. Для `cancelled` без предметного итога result не создаётся. Авторитетная строка живёт по системному lifecycle Hub и до завершения либо при отсутствии предметного результата может иметь также `queued`, `running` или системный `unknown`. Предметный итог вроде `already_exists` или `not_eligible` записывается в обычную scalar-колонку, а не подменяет lifecycle.

Hub сам добавляет в видимую строку label, публичный EVM address, авторитетный lifecycle status и время. Автор не дублирует эти поля в `columns`. Сводку success/partial/error Hub строит по `run_account_states`, а не по тексту log, title или произвольному plugin status.

Схема `output` фиксируется в snapshot запуска: обновление или удаление софта не меняет смысл исторической таблицы. Изменение `primary_kind`, смысла ключа, его типа или агрегации — breaking change схемы и требует нового kind либо major-версии софта.

Вкладка **Результаты** показывает account table, поиск по label/address, фильтр lifecycle status и объявленные агрегаты. Полную видимую проекцию до 2 000 строк можно скачать как formula-safe CSV; если report response помечен `truncated`, Hub ОБЯЗАН заблокировать CSV, а не экспортировать неполный набор. В строковых ячейках первый `=`, `+`, `-`, `@`, tab, CR или LF нейтрализуется ведущим апострофом. Schema-typed `integer`, `number` и `decimal_string` сохраняются числами без апострофа, поэтому корректное отрицательное значение не превращается в текст. Это деклассификация метаданных: export доступен только при открытом Vault, не включает произвольные undeclared payload fields и после скачивания больше не защищён Vault.

## 10. Безопасное логирование и скачиваемый журнал

### 10.1. Абсолютное правило автора

Секрет ЗАПРЕЩЕНО передавать в любой наблюдаемый канал даже в расчёте на redaction. Запрет распространяется на:

- `message` и `data` всех SDK events;
- results и return summary;
- exception text и traceback;
- stdout, stderr и Python logging;
- URL, query string, headers и request/response dumps;
- имя/путь файла, имя thread/process и CLI arguments;
- scratch, HAR, screenshot, DOM/HTML dump и crash report;
- метрики, hashes, base64/hex, маскированные фрагменты и производные значения, если по ним можно проверить или восстановить secret.

Любой project-specific referral/invite code и глобальные API keys считаются secret-bearing независимо от того, опубликовано ли значение внешним сервисом. Их ЗАПРЕЩЕНО включать в message/data/result/summary/exception/`print` даже частично; runtime code до любого вывода ОБЯЗАН зарегистрировать полное значение через `context.protect_secret(...)`.

Нельзя логировать `repr(context)`, `repr(account)`, HTTP client/request, browser capabilities, Selenium command payload или полный response object. Разрешены только заранее сформированные безопасные сообщения и allowlisted public fields.

Исключения сторонних SDK ОБЯЗАНЫ переводиться в стабильные безопасные коды, например `adspower_unavailable`, а не интерполироваться через `f"{error}"`. Raw exception разрешено использовать только в локальном тесте без реальных secrets.

Это требование включает внешнюю границу entrypoint. Bootstrap 0.6.22 при необработанном исключении формирует failed-message из имени и текста exception и печатает traceback в stderr; redactor после этого является лишь последней защитой. Поэтому production-entrypoint ОБЯЗАН перехватить ожидаемые ошибки клиентов/SDK, классифицировать их в заранее определённый safe code/message без raw exception и завершить lifecycle каждого уже начатого аккаунта. Неизвестное исключение нельзя интерполировать или помещать в `data`; его безопасно классифицируют общим кодом, а детальную диагностику воспроизводят без production secrets. `except Exception: pass` и ложный success ЗАПРЕЩЕНЫ.

### 10.2. Defense-in-depth Hub

Скачиваемый технический журнал 0.6.22:

- доступен через token-authenticated endpoint конкретного run и является одним общим журналом софта по всем выбранным аккаунтам, а не набором per-account файлов;
- сохраняет единый порядок событий run; account-scoped событие связывается с аккаунтом через `account_id` и безопасный snapshot `account_label`;
- отдаётся как bounded UTF-8 JSONL attachment;
- не включает manifest и options;
- повторно пропускает сохранённые events и run metadata через redactor;
- скрывает известные secret fields (включая referral/referrer codes), exact выданные значения, private-key patterns, proxy, email, Authorization, Cookie, JWT и high-entropy tokens;
- ограничен `16 MiB`, имеет footer с количеством пропущенных событий;
- возвращает `Cache-Control: no-store` и `X-Soft-Hub-Redacted: true`.

Эта защита не обнаруживает все кодировки, разбиение секрета по нескольким событиям, нестандартный формат, изображение или содержимое файла. Автор всё равно несёт ответственность за правило 10.1. Скачанный журнал следует считать чувствительным operational artifact и не публиковать без ручной проверки.

### 10.3. Правильные сообщения

Правильно:

```python
context.log(
    "AdsPower-профиль готов к подключению",
    account_id=account.id,
    data={"attempt": attempt, "service": "adspower"},
)
```

ЗАПРЕЩЕНО:

```python
context.log(f"profile={profile_id} api_key={api_key} email={email}")
context.log(f"request failed: {error}")
```

## 11. Канонический workflow AdsPower

Transport, авторизацию и актуальные поля ответа автор ОБЯЗАН сверять с первичной документацией AdsPower: [обзор Local API](https://localapi-doc-en.adspower.com/docs/Rdw7Iu), [получение профиля](https://localapi-doc-en.adspower.com/docs/u8m2Ie) и [официальные примеры](https://localapi-doc-en.adspower.com/docs/K4IsTq). Hub намеренно не фиксирует в своём SDK конкретный localhost endpoint или способ авторизации: они принадлежат интеграции плагина и могут меняться независимо от контракта Hub.

### 11.1. Декларация

Любой AdsPower action ОБЯЗАН одновременно объявить:

- `permissions.browser: true`;
- `permissions.local_services` с точным значением `adspower`;
- action permission `adspower_profile`;
- action permission `adspower_api_key`;
- `resources.account: [ ..., "adspower_profile" ]`;
- `resources.settings: [ ..., "adspower_api" ]`.

Если сценарий использует Capsolver, он дополнительно ОБЯЗАН объявить `capsolver_api_key` и resource `capsolver`. Email, Twitter, proxy и private key объявляются только если их реально читает выбранное действие.

### 11.2. Последовательность на каждый аккаунт

До создания workers entrypoint ОБЯЗАН один раз прочитать AdsPower profile ID всех выбранных аккаунтов в памяти, проверить непустоту и найти точные дубли. Один profile ID нельзя назначать двум выбранным Hub accounts: оба конфликтующих аккаунта завершаются `blocked` до вызова AdsPower, а само значение никуда не логируется. Актуальный Vault запрещает новый точный дубль при импорте и редактировании, но defensive-проверка плагина остаётся обязательной для legacy/migrated БД и внешних изменений. Hub leases привязаны к `account_id` и сами по себе общий внешний профиль не распознают.

Канонический flow:

1. Проверить `context.check_cancelled()`.
2. Получить profile ID через `account.secret("adspower_profile")`, а общий API key через `context.settings.secret("adspower_api")`; проверить непустоту и допустимую длину, не логируя значения.
3. Выполнить bounded read-only health/auth check локального AdsPower API.
4. Убедиться, что profile ID разрешается ровно в один профиль и профиль доступен. Ноль или несколько совпадений — `blocked` до side effect.
5. Зафиксировать, был ли профиль уже запущен до текущего run.
6. Запустить профиль через официальный локальный AdsPower API только при необходимости; сохранить returned connection endpoint только в памяти.
7. Подключиться к returned WebDriver/CDP endpoint. ЗАПРЕЩЕНО копировать browser profile directory или запускать отдельный Chrome с этими данными.
8. Выполнить сценарий с timeout на навигацию, ожидание DOM и каждый внешний запрос. Перед каждым submit/click и между retry проверить cancel.
9. Сразу после потенциального внешнего эффекта считать состояние `write_may_have_happened`, пока результат не подтверждён чтением/API/DOM.
10. В `finally` закрыть созданные run вкладки, detach driver, завершить свои workers и остановить профиль только если его запустил текущий run.
11. Не останавливать профиль, который был открыт до run, и не завершать чужой browser process.
12. Если stop/detach/confirmation не дали однозначного результата после возможного write, terminal status ОБЯЗАН быть `failed` для ошибки или `cancelled` для явной остановки; message ОБЯЗАНО сказать, что перед повтором нужно проверить внешний outcome по business/public operation ID.

AdsPower API key является общей настройкой `context.settings`, а profile ID — отдельным секретом `HubAccount`. Capsolver также является общей настройкой `context.settings`. Их нельзя менять местами, хранить в options или кэшировать в plugin-owned файле.

Proxy для браузерного сценария СЛЕДУЕТ настраивать в самом AdsPower profile. Плагин ЗАПРЕЩЕНО незаметно менять постоянный proxy/fingerprint/profile configuration. Отдельное действие, которое делает такую мутацию, ОБЯЗАНО называться явно и иметь `risk: external_write`.

Hub не устанавливает AdsPower и не управляет его лицензией. Host preflight до subprocess проверяет только configured-флаги AdsPower API key/profile ID и не делает live request к локальному сервису. Поэтому entrypoint ОБЯЗАН до первого browser/write side effect выполнить bounded live preflight: доступность Local API, авторизацию, однозначное существование каждого profile и совместимость returned endpoint. Недоступный сервис, неверный API key, неизвестный/дублирующийся profile ID дают понятный `blocked`, а не просьбу искать причину в terminal log.

### 11.3. Cancellation

Browser waits ОБЯЗАНЫ быть короткими или разбитыми на polling с cancel checks. Нельзя зависать в одном WebDriver call дольше объявленного timeout.

Плагин ЗАПРЕЩЕНО оставлять detached driver, browser child process, thread или async task. На Windows cooperative signal может быть менее надёжным, поэтому cleanup, идемпотентность и внешняя проверяемость операции ОБЯЗАНЫ быть рассчитаны также на force termination.

## 12. Минимальный entrypoint: lifecycle, AdsPower и безопасная отмена

Пример показывает контракт orchestration. `AdsPowerClient` и `automate_claim` должны быть реализованы и протестированы внутри пакета; они не могут печатать secrets или запускать detached processes.

```python
from __future__ import annotations

from soft_hub.sdk import CancelledError, HubAccount, HubContext

from plugin.adspower import AdsPowerClient, automate_claim


def run(context: HubContext) -> dict:
    if context.action_id != "claim_reward":
        raise ValueError("unsupported_action")

    max_attempts = context.options.get("max_attempts", 1)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("invalid_max_attempts")

    counters = {
        "total": len(context.accounts),
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "blocked": 0,
        "cancelled": 0,
    }
    for account in context.accounts:
        context.check_cancelled()
        status = _run_account(context, account, max_attempts)
        counters[status] = counters.get(status, 0) + 1
    return counters


def _run_account(context: HubContext, account: HubAccount, max_attempts: int) -> str:
    context.account_state(
        account.id,
        status="running",
        stage="preflight",
        progress=0.01,
        message="Проверяем browser-ресурсы",
    )

    try:
        profile_id = account.secret("adspower_profile")
        api_key = context.settings.secret("adspower_api")
        email = account.secret("email")
        capsolver_key = context.settings.secret("capsolver")
    except KeyError:
        context.account_state(
            account.id,
            status="blocked",
            stage="resource_missing",
            message="Не заполнен обязательный ресурс профиля",
        )
        return "blocked"

    client = AdsPowerClient(api_key=api_key, timeout_seconds=15)
    # Ключ детерминирован предметной операцией, а не run_id:
    # тот же claim после restart обязан получить тот же business key.
    business_key = client.reward_business_key(account.evm_address)
    session = None
    write_may_have_happened = False
    cancelled: CancelledError | None = None
    terminal_status = "failed"
    terminal_stage = "automation_failed"
    terminal_message = "Сценарий завершился известной ошибкой"
    safe_result: dict = {}

    try:
        context.check_cancelled()
        client.require_ready_profile(profile_id)
        context.account_state(
            account.id,
            status="running",
            stage="browser_starting",
            progress=0.10,
            message="Запускаем изолированный browser-профиль",
        )
        session = client.start_or_attach(profile_id)
        context.check_cancelled()

        context.account_state(
            account.id,
            status="running",
            stage="automation",
            progress=0.30,
            message="Выполняем заявленный сценарий",
        )
        # Callback вызывается непосредственно перед первым submit/click.
        def mark_external_write() -> None:
            nonlocal write_may_have_happened
            context.check_cancelled()
            write_may_have_happened = True

        safe_result = automate_claim(
            session=session,
            email=email,
            capsolver_key=capsolver_key,
            max_attempts=max_attempts,
            idempotency_key=business_key,
            before_submit=mark_external_write,
            check_cancelled=context.check_cancelled,
        )
        context.account_state(
            account.id,
            status="running",
            stage="result_confirmed",
            progress=0.85,
            message="Итог сценария подтверждён",
        )
        # Функция возвращается только после независимого подтверждения итога.
        write_may_have_happened = False
        terminal_status = "succeeded"
        terminal_stage = "completed"
        terminal_message = "Награда подтверждена"
    except CancelledError as error:
        cancelled = error
        terminal_status = "cancelled"
        terminal_stage = "external_outcome_unknown" if write_may_have_happened else "cancelled"
        terminal_message = (
            "Запуск остановлен. Проверьте внешний итог перед повтором"
            if write_may_have_happened
            else "Работа остановлена до внешнего действия"
        )
    except Exception:
        terminal_status = "failed"
        terminal_stage = "external_outcome_unknown" if write_may_have_happened else "automation_failed"
        terminal_message = (
            "Ошибка после внешнего действия. Проверьте его итог перед повтором"
            if write_may_have_happened
            else "Сценарий завершился известной ошибкой"
        )
    finally:
        try:
            if session is not None:
                client.detach_and_stop_if_owned(session)
        except Exception:
            terminal_status = "failed"
            terminal_stage = "external_outcome_unknown"
            terminal_message = (
                "Не удалось подтвердить browser cleanup. "
                "Проверьте внешний итог перед повтором"
            )

    if terminal_status == "succeeded":
        context.result(
            "Награда подтверждена",
            kind="reward_claim",
            status="succeeded",
            account_id=account.id,
            data={"confirmed": True, "attempts": safe_result.get("attempts", 1)},
        )

    context.account_state(
        account.id,
        status=terminal_status,
        stage=terminal_stage,
        progress=1.0 if terminal_status == "succeeded" else None,
        message=terminal_message,
    )
    if cancelled is not None:
        raise cancelled
    return "succeeded" if terminal_status == "succeeded" else "failed"
```

В production-коде generic `Exception` ЗАПРЕЩЕНО выводить в event. На внешней границе его нужно классифицировать в собственную ошибку с безопасным кодом. Неоднозначный write получает `failed` или `cancelled`, но его message/result не маскирует риск дублирования при повторе.

## 13. Риск, leases, идемпотентность и внешняя проверка

### 13.1. Leases

Непосредственно перед выполнением `testnet_write/mainnet_write` Hub резервирует выбранный account по каждому объявленному chain ID. Для `external_write` Hub резервирует отдельный per-account external-service scope. Если тот же scope/account уже занят, run остаётся queued, не занимает subprocess slot и автоматически продолжает после освобождения. Это предотвращает часть параллельных конфликтов между Hub runs без падения всей пачки.

Lease не гарантирует:

- отсутствие работы тем же аккаунтом вне Hub;
- уникальность AdsPower profile ID между разными Hub accounts;
- правильный фактический chain ID;
- атомарность нескольких внешних систем;
- отсутствие detached child process.

Автор ОБЯЗАН иметь собственные fail-closed проверки identity, chain, remote state и повторного запуска.

Lease живёт только пока run активен. При любом terminal outcome — `succeeded`, `failed`, `cancelled`, crash/restart recovery или force stop — Hub атомарно освобождает все leases и pins run. Ручная проверка/review не является gate для повтора. Освобождённый lease не доказывает, что внешней мутации не было.

### 13.2. Идемпотентность

Каждая внешняя мутация ОБЯЗАНА иметь явно описанную стратегию идемпотентности:

- использовать remote idempotency key, если API его поддерживает;
- не повторять submit после timeout, пока внешний state не проверен;
- перед повтором читать chain/API/DOM и доказывать, что операция отсутствует;
- сохранять и показывать безопасный public operation ID/transaction hash;
- ограничивать retry и применять backoff с jitter;
- отделять retry чтения от retry записи.

`run_id` пригоден для дедупликации повторной попытки внутри одного run, но новый run получает новый ID. Поэтому защита между runs ОБЯЗАНА опираться на стабильный бизнес-ключ операции и durable public operation ID, а не только на `run_id`.

Host idempotency batch admission предотвращает повторное создание одной и той же пачки после неопределённого ответа API Hub. Request identity включает `execution_mode`; тот же key нельзя переиспользовать для другого состава или режима. Это не делает идемпотентными внешние действия плагинов.

Batch имеет два host-owned режима и не требует нового manifest flag:

- `parallel` — совместимые софты конкурируют за глобальные subprocess slots; write/exclusive conflicts ждут leases;
- `sequential` — Hub идёт по списку сверху вниз и запускает следующий run только после terminal-состояния **всех** более ранних ordinals. `failed` или `cancelled` не прерывает остаток очереди.

Sequential waiter ЗАПРЕЩЕНО считать запущенным plugin process: до своей очереди он не занимает global slot/account lease и не получает расшифрованный Vault payload. Плагин ОБЯЗАН одинаково корректно работать при одиночном, parallel и sequential запуске и не строить бизнес-логику на способе admission.

### 13.3. Остановка

Плагин ОБЯЗАН вызывать `context.check_cancelled()`:

- перед каждым аккаунтом;
- перед каждым внешним write;
- между retry;
- внутри длительного polling;
- до создания browser/child process;
- до перехода к следующему независимому шагу.

`CancelledError` нельзя поглощать через `except BaseException`. Cleanup выполняется в `finally`, после чего исключение пробрасывается. Любой cleanup имеет timeout.

Если write мог уйти наружу, cancel/force stop завершает run как `cancelled`, а crash/error/timeout — как `failed`. Плагин НЕ ДОЛЖЕН называть такой итог полностью отменённым. Он ОБЯЗАН сохранить доступный public operation ID/transaction hash и дать понятную инструкцию проверить explorer/API/DOM перед ручным повтором. Hub не удерживает lease и не требует подтверждения; ответственность за безопасный retry остаётся у плагина/оператора.

### 13.4. Опциональная read-only проверка

Софт с неоднозначными writes ОБЯЗАН иметь проверяемую external truth. Отдельное read-only действие проверки МОЖЕТ быть удобным интерфейсом, но Hub его не требует, не запускает автоматически и не связывает с ним возможность rerun.

Если такое действие есть, оно ОБЯЗАНО:

- иметь `risk: read` и не отправлять новую мутацию;
- восстанавливать истину из внешнего API/chain/DOM по стабильному business key или public operation ID;
- выдавать понятный result: `confirmed`, `not_found` или `unknown`;
- никогда не объявлять `not_found` по одному временно пустому ответу без достаточного подтверждения;
- прямо объяснять оператору, что результат проверки не скрывает историческую ошибку и не меняет её статус.

Scratch не является контрактным durable checkpoint storage. `state_model: resumable` разрешён только при наличии реального устойчивого journal/store, forward-only миграций и теста восстановления после kill/restart. Новая SemVer ОБЯЗАНА атомарно читать или преобразовывать состояние предыдущей schema до side effect; исправление неудачной миграции выпускается ещё одной, более высокой SemVer. Возврат к старому runtime не является recovery plan.

Новый verification run не получает через `HubContext` logs, results, summary, options, scratch или checkpoint предыдущего run. Поэтому он ОБЯЗАН восстанавливать истину из внешней системы по current account identity и durable business key/public operation ID, доступному независимо от transient контекста прошлого запуска. Если доказать итог невозможно, action честно возвращает `unknown`. Само значение `state_model: stateless` не доказывает идемпотентность повторного write, `externally_reconciled` не создаёт Hub gate, а `resumable` не создаёт storage.

## 14. Network и зависимости

### 14.1. Network

Софт ОБЯЗАН:

- обращаться только к hosts, перечисленным в `permissions.network`, и локальным сервисам из `local_services`;
- проверять TLS certificates; `verify=False` и глобальное отключение warnings ЗАПРЕЩЕНЫ;
- ставить connect/read/total timeout;
- иметь bounded retries;
- не делать silent direct fallback, если для аккаунта требуется proxy;
- не передавать credentials в URL, если API допускает header/body;
- не логировать URL, если внешний протокол всё же требует credential query parameter;
- проверять chain ID, contract addresses и environment до подписи;
- ограничивать response size и не сохранять raw response.

Wildcard allowlist и endpoint, управляемый недоверенной option, ЗАПРЕЩЕНЫ. Redirect на неожиданный host должен быть отклонён либо проверен по тому же allowlist.

### 14.2. Python dependencies

Если `runtime.requirements` объявлен, указанный файл ОБЯЗАН:

- закреплять прямые и транзитивные версии воспроизводимым lock/constraints-подходом;
- не использовать VCS URL, mutable branch, local absolute path и untrusted extra index;
- по возможности использовать hashes;
- проходить dependency vulnerability audit и license review;
- иметь wheels для каждой заявленной ОС/архитектуры либо доказанную build-процедуру;
- не содержать package, скачивающий browser/runtime/model при import.

`.venv` в пакет включать ЗАПРЕЩЕНО. Плагин не должен полагаться на библиотеки core Hub. Bootstrap удаляет core path после загрузки SDK, а plugin `.venv` имеет приоритет для зависимостей.

Hub подготавливает только Python requirements. Он не устанавливает AdsPower, Chrome, Playwright browser bundle, Node.js, системную библиотеку или драйвер. Browser-софт ОБЯЗАН использовать установленный AdsPower и возвращённый им supported connection/driver, либо явно блокировать запуск понятным preflight.

Runtime self-update, `pip install` во время run, динамическая загрузка executable, `shell=True`, `eval/exec` недоверенного текста, pickle недоверенных данных и detached daemon ЗАПРЕЩЕНЫ.

### 14.3. EVM-подпись и транзакции

Action с `evm_private_key` ОБЯЗАН выполнять подпись локально в памяти. Private key, seed и raw signed transaction ЗАПРЕЩЕНО отправлять в RPC/API/browser, помещать в URL, файл, result, summary или журнал. До каждой подписи код fail-closed проверяет как минимум:

- фактический `chainId` RPC и ожидаемую сеть;
- sender, destination contract/EOA, selector/calldata и отсутствие неожиданного delegate/proxy target;
- `value`, token, spender, amount, decimals и лимит approval;
- nonce, gas/fee limits и допустимые max-cost bounds;
- соответствие операции выбранному action и явно подтверждённому risk.

Unlimited approval, permit, arbitrary signature и blind signing для нового софта ЗАПРЕЩЕНЫ, если это не отдельное явно названное `mainnet_write` действие с точным описанием и acceptance review. До broadcast СЛЕДУЕТ выполнить доступную simulation/`eth_call`; после broadcast сохраняется только public chain ID, transaction hash и значения в явно указанных единицах. Ошибка/отмена между подписью, broadcast и receipt считается неоднозначной границей write: run завершается `failed` или `cancelled`, а оператор проверяет chain truth по public transaction hash перед повтором. Никогда не считать локально созданный hash доказательством включения транзакции.

### 14.4. Solana-подпись и транзакции

Action, которому нужен Solana wallet, ОБЯЗАН объявить одновременно `permissions.secrets: ["solana_private_key"]`, `resources.account: ["solana_private_key"]` и `compatibility.hub >=0.6.20`. Hub выдаёт через `account.secret("solana_private_key")` canonical base58 полного проверенного 64-byte Ed25519 keypair. Seed, mnemonic, JSON-массив и публичный адрес как замена secret contract не поддерживаются.

Ключ разрешено декодировать только локально в памяти непосредственно для подписи. Его, seed bytes, secret/public keypair array и raw signed transaction ЗАПРЕЩЕНО отправлять в RPC/API/browser, URL, файл, result, summary, exception или журнал. До подписи код fail-closed проверяет genesis hash/cluster, актуальный blockhash, fee payer, каждую program ID, account metas, writable/signers, instruction data, lamports/token amount, mint/authority/delegate и верхнюю границу fee. Для versioned transaction ОБЯЗАТЕЛЬНО проверить address lookup tables и итоговый resolved message; blind-signing сериализованного payload от внешнего API ЗАПРЕЩЁН. После broadcast сохраняются только публичная signature, cluster и безопасные предметные поля. При timeout/отмене после возможной отправки оператор проверяет signature status и account state до повтора.

## 15. UX-контракт

Софт считается частью одного продукта и ОБЯЗАН говорить с пользователем через Hub, а не через терминал.

Требования:

- `presentation.display_name` — человеческое имя без технического суффикса `bot/script/patch`, если он не часть бренда;
- короткое описание объясняет одно основное назначение;
- полное описание сообщает ресурсы, внешний эффект, сети, ограничения и итог;
- action name начинается с понятного действия: «Проверить», «Запустить», «Получить», «Сверить»;
- action description прямо говорит, только ли это чтение и что изменится;
- option labels содержат единицы измерения и безопасный default;
- ошибки preflight сообщают недостающий ресурс до запуска;
- account message отвечает «что происходит сейчас», не показывает machine exception;
- result отвечает «что получилось»;
- ошибка/отмена после возможного write объясняет, какую внешнюю систему и какой public operation ID проверить перед повтором;
- нормальная работа не требует открыть technical log;
- `input()`, terminal prompt, ручное редактирование config и просьба установить Python пользователю ЗАПРЕЩЕНЫ.

Тон интерфейса — прямой, спокойный и человеческий. Допустимо «Выберите аккаунты», «Софт уже работает», «Не хватает прокси». Не допускаются холодные конструкции вроде «lifecycle-проекция недоступна», искусственные англоязычные badges, подтверждения с перепечатыванием фраз и ложная специализация общего риска: например, `external_write` нельзя всегда описывать как изменение waitlist, если действие может менять другой внешний сервис.

Иконка и обложка не должны имитировать системные кнопки Hub и не должны содержать status badge, который устареет.

## 16. Сборка, checksums и GitHub release

### 16.1. Сборка

Архив ОБЯЗАН собираться из корня репозитория Hub штатной командой, а output — находиться вне source-каталога:

```bash
python3 scripts/build_plugin.py path/to/my-soft dist/my-soft-1.0.0.softhub.zip
```

Builder:

- валидирует manifest тем же Python-валидатором;
- исключает `.git`, `.venv`, `__pycache__`, `.DS_Store`;
- запрещает symlink и известные secret material files;
- сортирует entries и ставит фиксированное ZIP timestamp;
- вычисляет SHA-256 каждого файла;
- генерирует `hub.checksums.json`;
- атомарно заменяет output.

Builder не импортирует entrypoint, не создаёт plugin `.venv`, не проверяет совместимость wheels/целевой платформы и не выполняет полный installer pass над уже созданным ZIP. В частности, проверка assets payload, archive size/ratio и полного checksum-набора завершается при реальной установке. Поэтому build success не является приёмкой: автор ОБЯЗАН установить точный финальный asset в чистый Hub и выполнить smoke из installed path.

`hub.checksums.json` ОБЯЗАН содержать ровно одну lowercase SHA-256 сумму каждого файла архива, кроме самого `hub.checksums.json`. Ручное редактирование checksums ЗАПРЕЩЕНО.

После сборки автор ОБЯЗАН установить и протестировать именно получившийся byte-for-byte asset. Проверка checksum не доказывает авторство; внешний SHA-256 release asset ОБЯЗАН публиковаться по доверенному каналу.

### 16.2. GitHub и Patch Radar

Для автоматического обнаружения:

1. Repository ОБЯЗАН быть public.
2. Имя repository ОБЯЗАНО оканчиваться точно на `.patch` без учёта регистра.
3. Release tag ОБЯЗАН соответствовать manifest version: `v1.2.3` для `1.2.3`.
4. Latest Release ОБЯЗАН содержать ровно один installable asset с суффиксом `.softhub` или `.softhub.zip` без учёта регистра.
5. Рекомендуемое имя asset: `<plugin-id>-<version>.softhub.zip`.
6. Обычный source archive GitHub не является пакетом Hub.
7. Asset не заменяется после публикации; исправление выпускается новой SemVer.
8. Release notes ОБЯЗАНЫ содержать SHA-256, список изменений, новые permissions/resources и migration/external-state recovery notes.
9. Manifest `version`, release tag и единственная SemVer в имени asset ОБЯЗАНЫ обозначать одну версию; неоднозначное имя или конфликт tag/filename блокирует безопасное сравнение.

Patch Radar просматривает не более первых 100 public repositories владельца и читает latest release metadata. Он не подтверждает личность автора и не подписывает пакет.

При первой GitHub-установке core скачивает и инспектирует архив, после чего сохраняет неизменяемую привязку `owner/repository ↔ plugin id` и для конкретной версии — release tag, asset name/URL и archive SHA-256. Renderer не назначает эту identity. После появления привязки Radar сравнивает candidate с самой высокой SemVer, когда-либо известной Hub для этого `plugin id`, и показывает одно из состояний:

- exact version — уже установлена, повторная кнопка установки не показывается;
- newer candidate — доступно обновление;
- newer installed — candidate является downgrade, установка блокируется;
- removed current / removed newer known — модуль удалён, а candidate совпадает с version floor либо ниже него; кнопка установки не показывается;
- removed update available — модуль удалён, candidate строго выше version floor и его можно установить из прежнего repository;
- version unknown — release/tag/filename нельзя надёжно сопоставить, установка блокируется до исправления metadata;
- identity conflict — repository, plugin id или release asset противоречат сохранённой привязке, установка блокируется.

Новый непривязанный repository остаётся installable только как первичная инспектируемая установка. После download manifest остаётся окончательным источником `id/version`: несогласованная identity отклоняется. Дальнейшая установка строго forward-only — candidate SemVer ОБЯЗАНА быть выше любой уже известной Hub версии этого `plugin id`, включая неактивные строки и tombstones удалённого модуля. Exact current version считается уже установленной и не запускает установку, историческая версия не реактивируется, downgrade и повторное использование версии ЗАПРЕЩЕНЫ. Публиковать другое содержимое под прежней версией, tag или asset identity ЗАПРЕЩЕНО; это immutable-payload conflict. Любое исправление выпускается новой, более высокой SemVer.

### 16.3. Forward-only обновление

У Hub нет пользовательского механизма возврата версии. Установка модуля после первичной версии разрешена только при SemVer, строго большей самой высокой уже известной, и сразу делает новый пакет активным. Совпадающая активная версия означает «уже установлено»; известная историческая или удалённая версия не запускается повторно. Downgrade, повторное использование SemVer и замена payload под прежними version/tag/asset ЗАПРЕЩЕНЫ.

Любое исправление кода, manifest, assets, requirements или схемы plugin-owned state выпускается отдельной более высокой SemVer. Миграция plugin-owned state ОБЯЗАНА быть forward-only, атомарной и fail-closed: новая версия сначала проверяет формат, затем преобразует его до внешнего side effect, а при неизвестной schema ничего не изменяет и сообщает безопасную ошибку. Hub не мигрирует это состояние за плагин.

## 17. Что проверяет Hub, а что остаётся обязанностью автора

| Контроль | Автоматически в текущем коде 0.6.22 | Обязанность автора/приёмки |
|---|---|---|
| Manifest shape и неизвестные поля | Да; `/5` требует safety-поля и полную карточку `presentation`, но допускает безопасные defaults для catalog и UI-подсказок options; `/4` сохраняет strict форму | Не использовать legacy admission для обхода safety invariants. |
| Catalog sections | Да; `/5` допускает отсутствие или независимое placement, `/4` сохраняет cross-check, все версии получают immutable run snapshot | Не считать раздел permission, network gate или доказательством риска. |
| Presentation paths/payload/byte limits | Да; `/5` и `/4` требуют `presentation`, legacy может его не иметь | Геометрия, статичность, metadata и качество проверяются acceptance review. |
| Resources vocabulary и связь с permissions | Да; `/5` и `/4` требуют точное двустороннее соответствие resources ↔ action secrets | Объявлять только реально читаемые значения и тестировать каждый missing resource. |
| Referral topology/runtime | Да с 0.6.5: encrypted child→parent forest, revision/CAS, atomic cycle validation, exact direct-parent grants, pins и optional exclusive lease | Получать/кэшировать/подставлять project code в плагине, сразу вызывать `protect_secret`, соблюдать dependency order и не сохранять code. |
| AdsPower browser/local service declaration | Да для AdsPower secret permissions | Реальный endpoint allowlist, workflow, cleanup и отсутствие утечки. |
| Secret выдача по action permissions | Да | Запрос минимальных прав и отсутствие самостоятельного чтения файлов. |
| Наличие конкретных account/global values до spawn | Да в релизе 0.6.5 | Повторный fail-closed check в entrypoint; acceptance test каждого missing resource. |
| Network list | Проверяется форма | Фактический allowlist не sandboxed; код обязан соблюдать список. |
| Risk/financial risk/chains | Проверяется декларативная согласованность | Честная классификация фактического кода и chain checks. |
| Options | `/5` принимает закрытую primitive schema с необязательными UI hints; `/4` сохраняет strict metadata; runner до Vault проверяет unknown/required/type/enum/bounds/step/length | Предметный формат, security policy и безопасные defaults повторно проверяются entrypoint/tests. |
| Account concurrency | `/5` разрешает отсутствие option с safe effective `1` и универсальным host maximum `20`; строгий `/4` дополнительно ограничивает browser maximum `5`; effective value clamp-ится по selected count | Если параллелизм объявлен, фактически использовать `context.account_concurrency`/`map_accounts`; для browser выбрать проверенный project-safe предел `1..5`, обеспечить thread safety, cancellation и cleanup. |
| Batch scheduling | Атомарный admission, idempotency с mode, `parallel|sequential`; sequential barrier стоит до slot/lease/Vault payload | Не менять предметную семантику action в зависимости от режима; внешние writes остаются самостоятельно идемпотентными. |
| Account lifecycle/progress | Проверяются protocol, membership, диапазон, монотонность, terminal правила, AVG по аккаунтам и сохранение последнего milestone при ошибке | Смысл status/stage, честные веса и покрытие каждой ветки. |
| Result semantics | Проверяются JSON/bounds, redaction и принадлежность `account_id` | Стабильный `kind/data`, один финальный предметный итог и согласованность с lifecycle. |
| Log redaction/export bounds | Да, defense-in-depth | Никогда не эмитить secret; redactor не DLP. |
| Leases и force-stop ambiguity | Да по объявленному risk; terminal transition атомарно освобождает leases/pins | Идемпотентность, durable external truth, business key/public operation ID и безопасный retry. |
| Review/hide terminal/account errors | Да, без удаления error/events/results/account states; не является rerun gate | Не путать скрытие notification с проверкой внешнего outcome и не объявлять исходную ошибку успехом. |
| GitHub source/version identity | Да после первой core-inspected установки: repository↔id, SemVer state и immutable archive hash | Согласовать manifest/tag/filename, не переиспользовать version/payload и публиковать новую SemVer для любого изменения. |
| Python environment | Version-local `.venv` и marker | Pinning, supply-chain audit, platform wheels. |
| OS/network isolation | Нет | Устанавливать только доверенный код; не заявлять наличие sandbox. |

## 18. Обязательная программа испытаний

### 18.1. Package и manifest

- [ ] Builder завершается успешно без ручной правки ZIP.
- [ ] Архив устанавливается в чистый data directory.
- [ ] Новый manifest содержит `contract_version: SH-SOFTWARE-0.6/5` и не использует legacy marker для обхода safety checks.
- [ ] `id` постоянен, version увеличена, compatibility.hub не ниже `>=0.6.22`.
- [ ] Если `/5` объявляет catalog, sections unique и содержат только `general`, `nft`, `testnet`; placement проверено отдельно от action risks.
- [ ] `/5` без catalog использует testnet-risk→`testnet`, остальное→`general`; ни один NFT section не выводится из copy/assets/network/chains.
- [ ] Legacy fixtures доказывают fallback testnet-risk→`testnet`, остальные→`general`; ни один NFT section не выводится из copy/assets/network/chains.
- [ ] Run snapshot сохраняет `catalog_sections_json`; update и uninstall не перемещают исторические результаты.
- [ ] В ZIP нет wrapper directory.
- [ ] В ZIP присутствуют manifest, generated checksums, icon, image и entrypoint; файл requirements присутствует только когда на него ссылается `runtime.requirements`.
- [ ] Icon и image непустые и статические; icon квадратный, image корректно кадрируется, рекомендуемые `512×512` и `1600×900` соблюдены либо отступление визуально проверено.
- [ ] Secret scan source, archive и dependencies не находит credential material.
- [ ] Повреждение любого payload-файла приводит к отказу checksum.
- [ ] Пакет проходит size/path/Unicode/case-collision проверки.

### 18.2. Presentation и UX

- [ ] Название, короткое и полное описание отображаются без fallback.
- [ ] Icon и image загружаются после локальной и GitHub-установки.
- [ ] Assets читаемы в light/dark theme и при crop/responsive layout.
- [ ] Каждое действие понятно без technical log; для важных options явно заданы понятные `title`/`description`, а `/5` defaults проверены там, где hints опущены.
- [ ] Весь видимый manifest-copy прочитан редактором: живой русский язык, активный залог, короткие предложения, нет внутренних `module/action/payload/lifecycle/scope/permission/lease/venv` и англоязычных заглушек.
- [ ] `x-ui.order` уникальны, enum имеют полный `enum_labels`, а одна group не смешивает primary/advanced.
- [ ] Явные `slider` имеют safe default и сетку до 1000 шагов; каждый `dual_range` состоит ровно из `from`/`to`, имеет совпадающие bounds/step/UI metadata и `from <= to`.
- [ ] На основном уровне не более 7 параметров (целевой диапазон содержательной формы — 5–7); редкие настройки находятся в понятной advanced-группе.
- [ ] Defaults дают безопасное начальное состояние; required без default действительно требует явного выбора и одинаково редактируется в одиночной и пакетной формах.
- [ ] Если `/5` action поддерживает account parallelism, он объявляет не-required `account_concurrency` с safe default и maximum не выше host-предела `20`; для browser выбран и протестирован фактический предел `1..5`. Без поля проверен effective default `1`; `account_mode:none` его не имеет.
- [ ] Options нового run не наследуют значения предыдущего; batch показывает полную schema-форму отдельно для каждого софта и отправляет exact собранные значения именно его run.
- [ ] Никакой пользовательский flow не требует терминала или `input()`.

### 18.3. Resources и least privilege

- [ ] У каждого action есть exact `permissions.secrets` и `resources`.
- [ ] Для каждого secret permission есть ровно соответствующий account/settings resource и наоборот.
- [ ] Top-level secret union точен.
- [ ] Read/self-check action не получает write secrets без необходимости.
- [ ] Locked Vault блокирует secret-bearing action до spawn.
- [ ] Отсутствующий EVM/Solana private key, proxy, email, Twitter или AdsPower profile определяется до spawn.
- [ ] Отсутствующий Capsolver/AdsPower API key определяется до spawn.
- [ ] Referral-aware action с `parent_required:true` отклоняет target без direct parent до spawn; `false` покрывает root-ветку в плагине.
- [ ] Любой `/5` action, использующий host topology, имеет exact target grants/resources и отдельно exact parent grants/resources; AdsPower parent использует `exclusive`.
- [ ] Закрытый Vault возвращает `423` для одиночного и batch start до создания/replay run IDs.
- [ ] Если ресурс отсутствует у одного из нескольких аккаунтов, UI называет тип проблемы до запуска всей пачки.
- [ ] Секрет не принимается через options, env, файл или URL.
- [ ] Любой Solana-action требует Hub `>=0.6.20`, exact permission/resource и получает только canonical base58 полного keypair; seed/mnemonic/address не принимаются.

### 18.4. Lifecycle и results

- [ ] Протестированы 1, 2 и максимальное поддерживаемое число аккаунтов.
- [ ] Для `account_concurrency=1`, safe default и declared maximum измерено фактическое число simultaneous workers; effective value clamp-ится по числу targets и сохраняется в run.
- [ ] `map_accounts` сохраняет input-order results; expected failure одного account не ломает terminal lifecycle остальных; cancel/timeout не оставляет threads/sessions.
- [ ] Проверены оба batch mode: `parallel` запускает совместимые subprocess одновременно; `sequential` соблюдает видимый порядок, продолжает после failed/cancelled и до своей очереди не занимает slot/lease/Vault payload.
- [ ] Каждый начатый аккаунт получает `running` и ровно один terminal status; отменённые до начала аккаунты корректно проецируются Hub из `queued` в `cancelled`.
- [ ] Протестированы все и только допустимые terminal account statuses: `succeeded`, `partial`, `failed`, `blocked`, `skipped`, `cancelled`.
- [ ] Progress конечный, `0..1`, монотонный; первый `running` имеет `0 < progress <= 0.10`, а успешный итог — ровно `1.0`.
- [ ] Долгий action сохраняет в БД/UI минимум три различных фактических промежуточных значения между стартом и terminal; вариант только `0 → 100` проваливает приёмку.
- [ ] Для повторяемого этапа тест с разными объёмами доказывает формулу `base + span * completed_weight / total_weight` с denominator, зафиксированным до работы.
- [ ] На двух аккаунтах общий progress равен среднему их milestones; global event без `account_id` не завышает account-run.
- [ ] Известная ошибка, blocked/cancelled и неоднозначный write сохраняют последний подтверждённый процент, а не становятся косметическими `100%`.
- [ ] Долгое ожидание показывает heartbeat/stage с неизменным progress; тест подтверждает отсутствие таймерного «доползания».
- [ ] Неотправленный terminal event виден как `unknown`, а не ложный success.
- [ ] Каждый начатый аккаунт с предметным итогом имеет ровно один финальный account-scoped result; `kind/data` стабильны и status согласован с terminal account state.
- [ ] Parsing-action остаётся чистым `read`/`one_or_more`; любая мутация переклассифицирована в правильный write-risk.
- [ ] `output` с `mode: account_table`, если объявлен, имеет 1..12 прямых scalar-колонок, не более четырёх numeric aggregates и ровно один `primary_kind` result на каждый начатый кошелёк.
- [ ] CSV статистики блокируется при `truncated`, не содержит undeclared fields, нейтрализует formula-like строковые ячейки и сохраняет schema-typed числа числами.
- [ ] Summary содержит только агрегаты и совпадает с account states.
- [ ] NFT/WL action показывает реальные milestones preflight/auth/eligibility/submit либо quote/simulation/sign/broadcast/receipt/indexing; ожидание не симулирует рост таймером.
- [ ] NFT success получает `1.0` только после подтверждённого обязательного результата; локальная подпись, рассчитанный hash или browser click не принимаются за receipt/order confirmation.
- [ ] NFT/testnet table содержит только объявленные публичные scalar-поля; raw signature, signed transaction, полный OpenSea order, permit, cookie и authorization отсутствуют.

### 18.5. Логи и секреты

- [ ] В штатном и ошибочном пути нет `print`, raw traceback и verbose HTTP/browser logging.
- [ ] Canary secrets не появляются в events, results, summary, stderr, paths и scratch.
- [ ] Скачанный `.log` не содержит canary EVM/Solana private key или seed, proxy, email, passwords, API keys, referral/referrer codes, Authorization, Cookie и AdsPower profile ID.
- [ ] Manifest/options отсутствуют в скачанном журнале.
- [ ] Один скачанный журнал содержит события всех аккаунтов run в общем порядке и позволяет различить их по `account_id`/`account_label`.
- [ ] Большой журнал корректно завершается footer `truncated/omitted_events`.
- [ ] После run scratch не содержит secret-bearing файлов.

### 18.6. Stop, retry и внешняя проверка

- [ ] Cancel до spawn не читает secrets и не создаёт external effect.
- [ ] Cooperative cancel до write даёт `cancelled`.
- [ ] Cancel/force stop после возможного write даёт `cancelled`, timeout/crash/error — `failed`; ни один путь не объявляет write полностью отменённым без доказательства.
- [ ] Любой terminal outcome атомарно освобождает Hub leases/pins; ошибка и review/hide никогда не блокируют rerun.
- [ ] Retry чтения bounded; write не повторяется без внешней проверки.
- [ ] Duplicate API response/reconnect не создаёт повторную операцию.
- [ ] Для каждого write есть стабильный business key или durable public operation ID, по которому можно проверить explorer/API/DOM после потери transient context.
- [ ] Если плагин даёт опциональное verification action, оно только читает, различает `confirmed/not_found/unknown`, работает без transient context предыдущего run и не меняет его status.
- [ ] Review/hide идемпотентно скрывает notification, сохраняет error/events/results/account states и не влияет на rerun.
- [ ] После Hub restart оборванный run становится `failed`, его evidence сохраняется, а leases/pins освобождаются.
- [ ] WL submit классифицирован как `external_write`; mainnet mint/approval/list/order/sale сохраняют `mainnet_write` risk и те же preflight/grants/leases в одиночном и batch запуске.
- [ ] Перед NFT mainnet подписью тестами доказаны fail-closed checks official domain, chain, contract, calldata/recipient/value/quantity, gas/fee, token/spender/approval и marketplace intent.
- [ ] После неоднозначного NFT broadcast/order submit повтор запрещён до проверки explorer/OpenSea/API по durable public ID.

### 18.7. Реферальная сеть

- [ ] UI строит topology как rooted forest: roots сверху, descendants по уровням, каждая direct parent→child связь видна линией; поиск/выбор узла и inspector не сжимают карту до нечитабельной таблицы.
- [ ] `POST /api/accounts/referral-topology` требует full coverage текущих accounts и валидный `expected_revision`; duplicate/missing/unknown/self/cycle/stale CAS отклоняют весь batch.
- [ ] Re-import сохраняет parent-связь; удаление parent отсоединяет прямых children; plaintext export не содержит топологию.
- [ ] Upgrade migration при unlock безвозвратно удаляет legacy own/external code fields, сохраняет валидные Hub-parent links и не ставит marker, если транзакция откатилась.
- [ ] Runner выдаёт только direct parents выбранных targets и exact parent resources; targets/parents pinned, `exclusive` leased, а удаление pinned account отклоняется.
- [ ] Entrypoint берёт parent только через `context.referrals.parent_for(child.id)`/ограниченный `parents`, а зависимые targets обрабатывает по `context.referral_levels` parent-first.
- [ ] Способ получения project-specific code принадлежит `/5`-плагину: runtime fetch или bounded option отражают реальный workflow проекта; Hub не хранит code как Vault resource.
- [ ] Немедленно после fetch и до любых log/result/exception/`print` вызван `context.protect_secret(code)`; control-frame регистрирует code только в host memory и не persist-ится.
- [ ] Project code отсутствует в Vault resources и в events/results/summary/log/files/scratch; если `/5` принимает его как bounded option, плагин считает payload чувствительным и не выводит значение.

### 18.8. AdsPower

- [ ] Протестированы: сервис выключен, API key неверен, profile ID отсутствует, два выбранных аккаунта имеют одинаковый profile ID, профиль уже открыт.
- [ ] Configured preflight Hub отличён от bounded live preflight Local API; все эти ошибки дают понятный `blocked` до browser/write side effect.
- [ ] Протестированы: start timeout, attach failure, navigation timeout, cancel во время ожидания, stop failure.
- [ ] Профиль, открытый до run, не останавливается плагином.
- [ ] Профиль, открытый run, корректно закрывается в штатном пути.
- [ ] После cancel/ошибки не остаются owned tabs, driver, threads и child processes.
- [ ] Один AdsPower profile не используется одновременно двумя workers.
- [ ] API key, profile ID, connection endpoint и capabilities не попадают в журнал.

### 18.9. Dependencies и платформы

- [ ] Prepare проходит в чистом managed Python 3.12 environment.
- [ ] Все зависимости pinned и прошли vulnerability/license review.
- [ ] Нет runtime download/self-update/post-install surprise.
- [ ] Финальный пакет установлен и запущен из installed path на каждом target из `compatibility.os`: macOS arm64 для `darwin`, Windows 10/11 x64 для `win32`.
- [ ] Windows native dependencies доступны как бинарные `cp312-win_amd64` wheels; пакет не требует compiler/build toolchain на компьютере пользователя.
- [ ] Плагин не зависит от system Python, Node.js, Git, Microsoft Visual C++ Redistributable, shell PATH и библиотек core Hub.
- [ ] На обеих заявленных платформах stop/force-stop не оставляет detached child process; неоднозначный write после OS-specific force termination проверяется по durable external truth перед retry.

### 18.10. Release

- [ ] Локально протестирован именно финальный `.softhub.zip`.
- [ ] SHA-256 asset зафиксирован после тестов.
- [ ] GitHub repository оканчивается на `.patch`.
- [ ] Latest Release содержит ровно один `.softhub[.zip]` asset.
- [ ] Manifest version, tag и версия в asset filename согласованы и однозначно сравниваются по SemVer.
- [ ] Обновление использует SemVer строго выше любой уже известной Hub версии; downgrade, повторная установка исторической/удалённой версии и повторное использование SemVer отклоняются.
- [ ] Plugin-owned state проходит атомарную forward-only миграцию до side effect; исправление миграции выпускается новой SemVer.
- [ ] Release notes перечисляют permissions/resources/risk changes.
- [ ] Опубликованный version/tag/asset payload не перезаписывается и не переиспользуется с другим SHA-256; любое изменение получает новую SemVer.

## 19. Безусловные причины отклонения пакета

Пакет отклоняется без условного допуска, если выполнено хотя бы одно условие:

1. Нет `presentation`, raster icon или image либо используется legacy fallback.
2. Нет action-level `permissions.secrets` или `resources` у хотя бы одного действия.
3. Название, описание, action или risk не соответствуют фактическому поведению.
4. Секрет передаётся через options, env, файл, URL, log, result, summary, exception, path или scratch.
5. Архив содержит реальные accounts, cookies, HAR, browser profile, локальную БД, `.venv` или private-key payload.
6. Write замаскирован под `read`.
7. Browser action не объявляет AdsPower resources/permissions либо не имеет bounded cleanup.
8. Пользователь узнаёт об отсутствующем ресурсе только после запуска из technical log.
9. Entrypoint использует `input()`, raw stdout, shell, detached process или импортирует внутренние модули Hub.
10. Не обеспечен ровно один terminal account state на каждую обработанную ветку.
11. Плагин эмитит `needs_attention`, `reviewed`, `reconciled` или любой другой terminal account status вне списка `succeeded/partial/failed/skipped/blocked/cancelled`.
12. Нет стратегии идемпотентности, durable business/public operation ID и способа проверить external truth для write.
13. `safe_stop: true` заявлен без cancellation/cleanup tests.
14. Есть незакреплённые или недоверенные зависимости, runtime download либо критичная известная уязвимость.
15. Нет теста чистой установки, missing-resource preflight, stop, log privacy и заявленных платформ.
16. Изменён payload без увеличения SemVer или перезаписан опубликованный release asset.
17. Пакет требует ручной установки Python, Node.js, Git, VC++ runtime, compiler, редактирования config или терминала для штатного сценария.
18. Автор заявляет OS/network sandbox, цифровую подпись издателя или безопасность, которых Hub фактически не обеспечивает.
19. Новый пакет не объявляет `contract_version: SH-SOFTWARE-0.6/5` и не имеет явно обоснованной необходимости в legacy-контракте.
20. Объявленные options не являются закрытой primitive schema либо форма содержит Vault secret/security-critical policy; отсутствие необязательных `/5` UI hints само по себе не является отказом.
21. Не определена стабильная result schema, `output` с `mode: account_table` ссылается на вложенные/raw/секретные данные или progress тестируется только переходом `0 → 100`.
22. Action использует host referral topology с неполным `action.referral`, несовпадающими parent grants/resources или выбирает parent в обход `context.referrals`; runtime code persist-ится либо раскрывается.
23. Объявленный `/5` `account_concurrency` превышает host-предел `20`, входит в `required`, игнорируется кодом либо оставляет workers/sessions после cancel; browser-плагин не обосновал и не протестировал меньший безопасный предел. Для строгого `/4` browser maximum остаётся `5`.
24. Указанный catalog нарушает vocabulary/exclusivity либо софт пытается выдать placement за permission/risk proof; отсутствие catalog у `/5` допустимо.
25. NFT/WL write замаскирован под `read`, mainnet mint/list/order/sale имеет неверный risk, либо result/log сохраняет raw signature, signed transaction, marketplace order, cookie или authorization.

## 20. Definition of Done

Софт считается готовым к установке в Soft Hub 0.6.22 только когда одновременно:

- выполнены все MUST/MUST NOT этого документа;
- пройден весь checklist раздела 18;
- финальный архив собран штатным builder и повторно установлен из файла release candidate;
- UI показывает presentation и preflight без fallback/technical log;
- карточка и исторические результаты находятся в правильных catalog workspaces без дублирования run;
- безопасность логов проверена canary-тестами;
- write/stop/idempotency/external-verification semantics проверены аварийными тестами;
- владелец Hub одобрил permissions, risk, dependencies и release SHA-256.

Любое исключение из SHOULD оформляется отдельной записью с причиной, риском, компенсирующим контролем, тестом и сроком устранения. Исключений из MUST/MUST NOT нет: для них требуется новая версия этого контракта и согласованное изменение schema/runtime/tests.

## 21. Готовое задание для следующей сессии разработки

Если новый софт разрабатывает другой инженер или AI-ассистент, владелец Hub передаёт ему этот текст без сокращений:

```text
Адаптируй софт <SOURCE_PATH_OR_REPOSITORY> в новый пакет для Soft Hub.

Рекомендуемый контракт: SH-SOFTWARE-0.6/5 из docs/SOFTWARE_SPEC_RU.md.
До изменения кода полностью прочитай этот документ, schemas/plugin.schema.json,
soft_hub/sdk.py и scripts/build_plugin.py. Совместимость legacy не является
разрешением опускать presentation, action permissions или resources. Catalog и UI hints
объявляй только когда они нужны предметному интерфейсу.

Не запускай реальные аккаунты и не отправляй внешние write-операции без отдельного
явного разрешения. Не копируй секреты из исходного софта в пакет, fixtures, логи,
скриншоты или результаты.

Сначала выдай таблицу действий: optional catalog sections, action id, фактический внешний эффект, risk,
account_mode, exact secrets, account resources, global settings, network hosts,
chains, browser/local services, stop semantics, бизнес-ключ/public operation ID и способ
проверить external truth перед retry.
Если фактическое поведение нельзя доказать из кода, останови соответствующее
действие fail-closed и укажи blocker; не имитируй успех.

Затем реализуй полный immutable package с hub.plugin.json, optional requirements-файлом,
assets/icon, assets/image и plugin entrypoint. Manifest обязан явно содержать
contract_version SH-SOFTWARE-0.6/5 и compatibility.hub >=0.6.22.
Catalog необязателен; если он нужен, используй general отдельно либо nft/testnet/nft+testnet
и не считай placement разрешением или risk. Пользовательские параметры опиши закрытой
primitive options schema; UI hints добавляй там, где default Hub недостаточно понятен.
Если action поддерживает account parallelism, объяви account_concurrency с safe default,
maximum<=20, а для browser выбери и протестируй практический предел 1..5; фактически используй context.account_concurrency/map_accounts;
иначе положись на безопасный effective default 1. Секреты получай только через HubAccount/HubSettings.
Для referral-aware action объяви action.referral project_runtime, бери direct parent только через
context.referrals.parent_for/parents, обрабатывай зависимости по referral_levels, получай/подставляй
project code сам и немедленно вызови context.protect_secret(code) до log/result/exception/print.
Если реальный проект требует ручной code, объяви bounded /5 option. В любом варианте не сохраняй
code в Vault resources, events, results, summary, log, scratch или файлы.
Для каждого выбранного аккаунта выдай понятный lifecycle, ровно один terminal status,
стабильный предметный result и честный weighted progress с промежуточными milestones.
Для NFT/WL раздели read, submit, testnet mint и mainnet mint/list/order/sale по честным risks;
не сохраняй raw signatures, signed transactions или полные marketplace orders.
Не используй input(), raw print/logging, shell, runtime install/update или detached process.

До сдачи собери пакет только scripts/build_plugin.py, установи финальный архив
в чистый data directory и пройди весь checklist раздела 18, включая missing-resource,
canary-secret, cancel/force-stop, ambiguous-write/idempotency/external-verification, concurrency,
освобождение leases/pins на любом terminal path, опциональный review/hide без rerun gate,
чистую installed-package установку и заявленные OS/architecture.
Для Windows используй только совместимые cp312-win_amd64 wheels. Проведи dependency audit.

В ответе приложи:
1. путь к финальному .softhub.zip и его SHA-256;
2. итоговую таблицу permissions/resources/risk;
3. список выполненных тестов и их результат;
4. все честные ограничения и непроверенные внешние эффекты;
5. подтверждение, что MUST/MUST NOT выполнены без исключений.

Пакет не готов, пока обязательные safety-инварианты SH-SOFTWARE-0.6/5 не выполнены.
```

Перед началом новой major/minor-версии Hub владелец ОБЯЗАН сначала проверить, не появился ли более новый контракт. Номер `SH-SOFTWARE-0.6/5` нельзя автоматически переносить на несовместимый runtime.
