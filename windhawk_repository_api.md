# Windhawk Mod Repository — внешний API / формат репозитория

> Исследовано по исходному коду Windhawk и официальному репозиторию модов.
>
> **Важно:** это описание HTTP-интерфейса, который использует сам Windhawk. Отдельной публичной OpenAPI/REST-документации для этого интерфейса я не нашёл, поэтому endpoints ниже следует считать **внутренним/неформально документированным интерфейсом репозитория**, а не гарантированным публичным API.

## 1. Базовый URL

Корень репозитория:

```text
https://mods.windhawk.net/
```

В исходниках Windhawk он задаётся как `DEFAULT_MODS_URL_ROOT`.

Основные ресурсы находятся под:

```text
https://mods.windhawk.net/catalogs/
https://mods.windhawk.net/mods/
```

Windhawk также имеет официальный GitHub-репозиторий модов:

```text
https://github.com/ramensoftware/windhawk-mods
```

Официальный репозиторий описывает себя как коллекцию модов, которые можно просматривать в онлайн-каталоге и устанавливать через Windhawk.

---

# 2. Каталог модов

## GET `/catalogs/{language}.json`

Пример:

```http
GET https://mods.windhawk.net/catalogs/en.json
```

Другие языки имеют аналогичный формат:

```text
/catalogs/ru.json
/catalogs/de.json
/catalogs/fr.json
...
```

Конкретный набор поддерживаемых языков лучше получать из актуального каталога/исходников, а не хардкодить.

### Что делает Windhawk

Клиент сначала запрашивает:

```text
https://mods.windhawk.net/catalogs/{language}.json
```

Если сервер возвращает `404`, Windhawk делает fallback на:

```text
https://mods.windhawk.net/catalog.json
```

То есть логика:

```text
GET /catalogs/{language}.json
        │
        ├── 200 → использовать каталог
        │
        └── 404 → GET /catalog.json
                         │
                         └── использовать fallback
```

### Важная особенность

Windhawk передаёт JSON каталога практически без преобразования. Поэтому не стоит рассчитывать на конкретную форму JSON без проверки актуального ответа сервера.

Пример Python:

```python
import requests

url = "https://mods.windhawk.net/catalogs/en.json"

response = requests.get(url, timeout=15)
response.raise_for_status()

catalog = response.json()

print(catalog)
```

---

# 3. Кэширование каталога

Сам Windhawk использует HTTP `ETag`.

При первом запросе сервер может вернуть:

```http
ETag: "..."
```

При следующем запросе Windhawk отправляет:

```http
If-None-Match: "..."
```

Если каталог не изменился, сервер может ответить:

```http
304 Not Modified
```

и Windhawk использует ранее сохранённое содержимое.

Для собственного клиента это не обязательно, но если каталог запрашивается часто, рекомендуется реализовать `ETag`.

Пример:

```python
headers = {}

if etag:
    headers["If-None-Match"] = etag

response = requests.get(
    "https://mods.windhawk.net/catalogs/en.json",
    headers=headers,
    timeout=15,
)
```

---

# 4. Исходник мода

## GET `/mods/{mod_id}.wh.cpp`

Пример:

```http
GET https://mods.windhawk.net/mods/windows-11-taskbar-styler.wh.cpp
```

Где:

```text
{mod_id}
```

— ID мода.

Этот endpoint возвращает исходный `.wh.cpp` текущей версии мода.

Python:

```python
import requests

mod_id = "windows-11-taskbar-styler"

url = f"https://mods.windhawk.net/mods/{mod_id}.wh.cpp"

response = requests.get(url, timeout=15)
response.raise_for_status()

source = response.text

print(source)
```

---

# 5. Исходник конкретной версии

## GET `/mods/{mod_id}/{version}.wh.cpp`

Пример:

```http
GET https://mods.windhawk.net/mods/windows-11-taskbar-styler/1.2.3.wh.cpp
```

Где:

```text
{mod_id}  = ID мода
{version} = версия
```

Таким образом можно получить не только последнюю версию, но и старую.

Python:

```python
mod_id = "windows-11-taskbar-styler"
version = "1.2.3"

url = (
    f"https://mods.windhawk.net/mods/"
    f"{mod_id}/{version}.wh.cpp"
)

response = requests.get(url, timeout=15)
response.raise_for_status()

source = response.text
```

---

# 6. Список версий

## GET `/mods/{mod_id}/versions.json`

Пример:

```http
GET https://mods.windhawk.net/mods/windows-11-taskbar-styler/versions.json
```

Этот endpoint используется самим Windhawk для получения истории версий.

Python:

```python
mod_id = "windows-11-taskbar-styler"

url = f"https://mods.windhawk.net/mods/{mod_id}/versions.json"

response = requests.get(url, timeout=15)
response.raise_for_status()

versions = response.json()

print(versions)
```

Фактическую схему элементов `versions.json` следует считать серверной схемой и валидировать при разборе. В исходниках Windhawk она преобразуется в собственный список информации о версиях.

---

# 7. Metadata внутри `.wh.cpp`

Один из главных плюсов репозитория — дополнительный API для описания мода фактически не нужен.

Каждый мод представляет собой C++-файл с metadata-блоком:

```cpp
// ==WindhawkMod==
// @id              example-mod
// @name            Example Mod
// @description     Example description
// @version         1.0
// @author          Author
// @github          username
// @homepage        https://example.com
// @include         explorer.exe
// ==/WindhawkMod==
```

Официальная документация Windhawk описывает metadata как обязательную часть каждого мода и указывает, что она содержит как минимум информацию вроде ID и имени; дополнительные поля могут использоваться для автора, README и других сведений.

---

# 8. Основные metadata-поля

При разработке парсера полезно сохранять как минимум:

| Поле | Значение |
|---|---|
| `@id` | уникальный ID мода |
| `@name` | отображаемое название |
| `@description` | короткое описание |
| `@version` | версия |
| `@author` | автор |
| `@github` | GitHub автора/проекта |
| `@homepage` | домашняя страница |
| `@include` | процессы, для которых предназначен мод |

Не следует предполагать, что каждое необязательное поле присутствует у каждого мода.

---

# 9. README внутри `.wh.cpp`

Кроме короткого:

```text
@description
```

мод может содержать полноценный README-блок:

```cpp
// ==WindhawkModReadme==
/*
# Example Mod

Подробное описание мода.

## Usage

Инструкция по использованию.

## Settings

Описание настроек.
*/
// ==/WindhawkModReadme==
```

Поэтому для поисковика желательно хранить два отдельных поля:

```python
{
    "description": "...",
    "readme": "..."
}
```

`description` удобно показывать в результатах поиска, а `readme` — на странице конкретного мода.

---

# 10. Пример парсера metadata

Простейший вариант:

```python
import re

def parse_metadata(source: str) -> dict:
    result = {}

    match = re.search(
        r"//\s*==WindhawkMod==\s*(.*?)"
        r"//\s*==/WindhawkMod==",
        source,
        re.S,
    )

    if not match:
        return result

    block = match.group(1)

    for line in block.splitlines():
        match = re.match(
            r"\s*//\s*@([A-Za-z0-9_-]+)\s+(.*)",
            line,
        )

        if match:
            key, value = match.groups()
            result[key] = value.strip()

    return result
```

Использование:

```python
metadata = parse_metadata(source)

print(metadata.get("id"))
print(metadata.get("name"))
print(metadata.get("description"))
print(metadata.get("version"))
print(metadata.get("author"))
```

---

# 11. Парсинг README

Можно отдельно искать:

```text
// ==WindhawkModReadme==
```

и:

```text
// ==/WindhawkModReadme==
```

После извлечения желательно убрать начальные `//` с каждой строки.

Пример:

```python
import re

def parse_readme(source: str) -> str | None:
    match = re.search(
        r"//\s*==WindhawkModReadme==\s*(.*?)"
        r"//\s*==/WindhawkModReadme==",
        source,
        re.S,
    )

    if not match:
        return None

    lines = []

    for line in match.group(1).splitlines():
        line = re.sub(r"^\s*//\s?", "", line)
        lines.append(line)

    return "\n".join(lines).strip()
```

---

# 12. Рекомендуемая модель данных

Если задача — сделать собственный Windhawk search API / Discord-бота / веб-поисковик, удобно привести данные к единой структуре:

```json
{
  "id": "example-mod",
  "name": "Example Mod",
  "description": "Short description",
  "version": "1.2.3",
  "author": "Author",
  "github": "username",
  "homepage": "https://example.com",
  "include": [
    "explorer.exe"
  ],
  "readme": "# Example Mod\n\nLong description...",
  "source_url": "https://mods.windhawk.net/mods/example-mod.wh.cpp",
  "versions_url": "https://mods.windhawk.net/mods/example-mod/versions.json"
}
```

Это уже удобно отдавать через собственный REST API.

---

# 13. Поиск

У репозитория есть каталог, но отдельный документированный endpoint вида:

```text
GET /search?q=...
```

я не обнаружил.

Поэтому для собственного поиска самый надёжный вариант:

```text
GET /catalogs/en.json
        ↓
загрузить каталог
        ↓
проиндексировать его
        ↓
искать по ID / name / description
        ↓
при открытии мода:
GET /mods/{id}.wh.cpp
        ↓
получить полный metadata + README
```

Для большого количества запросов каталог лучше загрузить один раз и держать в памяти/локальной БД.

---

# 14. Рекомендуемая архитектура поисковика

```text
                  Windhawk Repository
                         │
                         ▼
              /catalogs/en.json
                         │
                         ▼
                  Catalog Loader
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
          In-memory              SQLite
           индекс                 индекс
              │                     │
              └──────────┬──────────┘
                         ▼
                    Search API
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
            Website           Discord Bot
```

Каталог можно периодически обновлять, например раз в несколько часов.

---

# 15. Получение полной информации о моде

Оптимальный алгоритм:

```text
1. Найти mod_id в каталоге.

2. Получить:
   /mods/{mod_id}.wh.cpp

3. Распарсить:
   @id
   @name
   @description
   @version
   @author
   @github
   @homepage
   @include

4. Распарсить:
   ==WindhawkModReadme==

5. Если нужна история:
   /mods/{mod_id}/versions.json

6. Если нужна конкретная версия:
   /mods/{mod_id}/{version}.wh.cpp
```

---

# 16. HTTP-ошибки

Практически полезно обрабатывать:

```text
200 OK
```

— ресурс найден.

```text
304 Not Modified
```

— каталог не изменился при использовании `ETag`.

```text
404 Not Found
```

— мод/версия/каталог отсутствует.

Для языкового каталога `404` имеет специальное значение: Windhawk пробует `/catalog.json`.

Для отсутствующего mod resource Windhawk рассматривает `404` как отсутствие мода в репозитории.

Другие сетевые ошибки следует считать ошибками доступа к репозиторию.

---

# 17. Ограничения и безопасность

ID мода и версия не следует бездумно принимать из пользовательского ввода и конкатенировать в URL.

Сам Windhawk перед построением URL валидирует:

```text
mod_id
version
```

через внутренние проверки допустимого формата.

Если делать публичный API, следует самостоятельно:

1. Валидировать `mod_id`.
2. Валидировать `version`.
3. Не разрешать произвольные URL.
4. Не превращать `mod_id` в возможность сделать SSRF.
5. Ограничить размер загружаемых `.cpp`/JSON.
6. Использовать timeout.
7. Кэшировать каталог.

---

# 18. Источник модов

Официальный репозиторий:

```text
https://github.com/ramensoftware/windhawk-mods
```

В нём моды хранятся как:

```text
mods/<mod-id>.wh.cpp
```

Репозиторий прямо указывает, что PR для нового мода должен содержать файл:

```text
mods/<mod-id>.wh.cpp
```

и что версия в metadata должна обновляться при выпуске новой версии.

---

# 19. Не путать с API самого Windhawk

Эти endpoints относятся к **репозиторию модов**:

```text
mods.windhawk.net
```

Это не тот же API, что C/C++ API, предоставляемый установленным Windhawk-модам.

Например, у самого Windhawk есть функции вроде:

```cpp
Wh_GetUrlContent(...)
```

для получения содержимого URL из кода мода, но это API моддинга, а не API каталога.

---

# 20. Минимальный клиент

Полностью достаточная основа для собственного клиента:

```python
import requests


class WindhawkRepository:
    BASE_URL = "https://mods.windhawk.net"

    def __init__(self, timeout=15):
        self.timeout = timeout
        self.session = requests.Session()

    def get_catalog(self, language="en"):
        url = f"{self.BASE_URL}/catalogs/{language}.json"

        response = self.session.get(
            url,
            timeout=self.timeout,
        )

        if response.status_code == 404:
            response = self.session.get(
                f"{self.BASE_URL}/catalog.json",
                timeout=self.timeout,
            )

        response.raise_for_status()

        return response.json()

    def get_source(self, mod_id, version=None):
        if version:
            url = (
                f"{self.BASE_URL}/mods/"
                f"{mod_id}/{version}.wh.cpp"
            )
        else:
            url = (
                f"{self.BASE_URL}/mods/"
                f"{mod_id}.wh.cpp"
            )

        response = self.session.get(
            url,
            timeout=self.timeout,
        )

        response.raise_for_status()

        return response.text

    def get_versions(self, mod_id):
        url = (
            f"{self.BASE_URL}/mods/"
            f"{mod_id}/versions.json"
        )

        response = self.session.get(
            url,
            timeout=self.timeout,
        )

        response.raise_for_status()

        return response.json()
```

---

# 21. Источники

- Windhawk — основной репозиторий:
  https://github.com/ramensoftware/windhawk

- Официальная коллекция модов:
  https://github.com/ramensoftware/windhawk-mods

- Документация создания модов:
  https://github.com/ramensoftware/windhawk/wiki/Creating-a-new-mod

- Онлайн-каталог:
  https://windhawk.net/

- Repository root:
  https://mods.windhawk.net/

## Важная оговорка

Структура выше основана на реализации repository client в исходниках Windhawk. Если разработчики поменяют backend, конкретные URL или JSON-схемы могут измениться без обязательства поддерживать обратную совместимость.

Наиболее стабильным ориентиром поэтому являются исходники самого Windhawk, а не сторонние описания API.
