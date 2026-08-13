# Pace Analysis — спецификация формата и доменной модели

Система: парсинг результатов картинговых гонок из писем **Apex Timing**, хранение в SQLite,
REST API (FastAPI) и веб-дашборд (Vite + React) с продвинутой статистикой темпа.

Единственный на сегодня эталонный вход:
`primo-karting-final-a.eml` (лежит в корне репозитория; данные получателя обезличены — см. §11).

---

## 1. Анатомия письма

Письмо — одночастное `text/html`, `Content-Transfer-Encoding: quoted-printable`, charset UTF-8.
Отправитель — тайминговая система Apex Timing от имени клуба (`From: "PRIMO KARTING" <info@primokarting.ru>`,
`Return-Path: returnpath@apex-timing.com`). Текстовой альтернативы (`text/plain`) нет.

### 1.1. Полезные заголовки

| Заголовок | Пример | Назначение |
|---|---|---|
| `Subject` | `PRIMO KARTING : PRIMO GARA - Final A (FA)​​​​​​​` | название клуба + название сессии. **Внимание:** содержит U+200B (zero-width space) и ` ` — нормализовать перед использованием. |
| `From` | `"PRIMO KARTING" <info@primokarting.ru>` | клуб / организатор |
| `To` | `"WLAD111" <driver@example.com>` | display-name = ник получателя в системе |
| `Date` | `Mon, 3 Aug 2026 20:52:30 +0200` | время отправки (таймзона **сервера Apex**, не площадки) |
| `Message-ID` | `<...@mx.google.com>` | ключ провенанса, дедупликация писем |

### 1.2. Структура HTML

Всё письмо — вложенные `<table>` (верстка рассылки). Данные лежат в «листовых» таблицах
(таблицах, внутри которых нет других `<table>`). В эталонном письме 27 таблиц, из них значимые:

```
[шапка клуба: телефон, сайт, facebook]
[«Hello WLAD111, your results !»]
[заголовок сессии]  PRIMO GARA - Final A (FA) - 03.08.2026 à 21:40 (Karting track)
[подиум: 3 карточки — место + ник]                       (дублирует классификацию, можно игнорировать)
[классификация]     Rnk | Kart | Driver | Laps | Gap | Best lap
[lap chart]         Kart | Driver | 1..N   (времена всех кругов всех пилотов)
[заголовок]         Your lap time SR5
[личные круги]      Lap | S1 | S2 | (пусто) | Time      (только для получателя письма)
[заголовок]         Your last sessions SR5
[история]           Rnk | Date | Best lap | Laps
[заголовок]         Best times of the week SR5
[рейтинг недели]    # | Driver | Best lap
[заголовок]         Track records SR5
[рекорды трассы]    # | Driver | Best lap
[футер: ссылка отписки]
```

`SR5` в заголовках секций — код категории/модели карта (Sodi SR5). Хранить как `category`.

### 1.3. Классификация сессии

Шапка: `Rnk | Kart | Driver | Laps | Gap | Best lap`.
Между строками данных встречаются пустые `<tr>` без ячеек — пропускать.
`Gap` у лидера — пустая строка. Возможные форматы гэпа (в других письмах): `2.022`, `1:05.412`, `1 Lap`, `2 Laps`.

Эталон (6 пилотов):

| Rnk | Kart | Driver | Laps | Gap | Best lap |
|---|---|---|---|---|---|
| 1 | 11 | KOLYA11 | 20 | | 26.012 |
| 2 | 2 | WLAD111 | 20 | 2.022 | 26.788 |
| 3 | 4 | TWG | 20 | 2.937 | 25.845 |
| 4 | 7 | DENISENKO | 20 | 5.372 | 26.341 |
| 5 | 5 | PHREEMAN | 20 | 10.730 | 26.359 |
| 6 | 19 | ИГОРЬ53 | 20 | 21.300 | 28.380 |

Ники могут быть кириллическими — не ломать кодировку и не транслитерировать.

### 1.4. Lap chart (главное — времена кругов)

Шапка: `Kart | Driver | 1 | 2 | ... | 10` — то есть таблица **свёрнута по 10 кругов в строку**
(ширина обёртки = число колонок-номеров в шапке, **не хардкодить 10**).

Один пилот = **несколько последовательных `<tr>`**:
* первая строка содержит `Kart` и `Driver`, дальше круги 1..W;
* каждая следующая строка того же пилота имеет **пустые** ячейки Kart и Driver, дальше круги W+1..2W;
* между пилотами встречается пустой `<tr>` (0 ячеек) — разделитель, но опираться только на него нельзя:
  признак нового пилота — непустая ячейка Driver.

Значения ячеек:
* `28.168` — время круга;
* `-` — времени нет (в эталоне это круг 1: старт с решётки/выезд, время не засчитано). Хранить как `None`,
  но **сохранять номер круга** (нумерация кругов = позиция ячейки, начиная с 1);
* строка последнего блока пилота может быть короче ширины обёртки, либо добиваться пустыми ячейками —
  пустые хвостовые ячейки игнорировать (они не создают круги).

**Семантика цвета:** ячейка лучшего круга пилота имеет `style` с `background-color:#515151` и `color:#FFFFFF`
(остальные — `#C0C0C0`). Это единственный источник признака «best lap» на уровне круга; использовать его
для флага `is_best`, но **не полагаться** на него как на единственную истину — сверять с колонкой `Best lap`
классификации (в эталоне совпадает у всех 6 пилотов).

### 1.5. Личные круги с секторами

Шапка: `Lap | S1 | S2 | <пустая колонка> | Time`. Число секторов зависит от трассы (`S1..Sk`) —
парсить динамически по шапке, пустые колонки-разделители отбрасывать.
Таблица есть **только для получателя письма** (ник из `To`/из приветствия `Hello X, your results !`).

Инварианты эталона: `S1 + S2 == Time` для всех кругов, где `Time` задано;
у круга 1 `Time = "-"` при заданных секторах (`56.053 / 14.243`) — не выдумывать сумму, хранить `None`.
Строка лучшего круга подсвечена целиком (`background-color:#515151`).

Времена кругов из lap chart и из личной таблицы для получателя **должны совпадать** —
парсер обязан это проверять и сообщать о расхождении (см. §4).

### 1.6. Прочие таблицы

* **Your last sessions** `Rnk | Date | Best lap | Laps` — история получателя, дата `dd.mm.yyyy`
  (без времени, за один день бывает несколько сессий).
* **Best times of the week** и **Track records**: `# | Driver | Best lap` — усечённые рейтинги
  (в эталоне: позиция 1 и «окно» вокруг получателя: 10–14 и 50–54). Различать по тексту заголовка над таблицей.

### 1.7. Метаданные сессии

Строка заголовка: `PRIMO GARA - Final A (FA) - 03.08.2026 à 21:40 (Karting track)`

Разбор: `<название сессии> - <dd.mm.yyyy> <разделитель> <HH:MM> (<трасса>)`, где разделитель —
`à` (fr) / `at` (en) / `в` (ru) / `um` (de). Название сессии само содержит ` - `, поэтому парсить
**с конца** регуляркой, а не split-ом по первому дефису. Суффикс в скобках внутри названия
(`Final A (FA)`) — код сессии, вынести в `session_code = "FA"`, `session_name = "PRIMO GARA - Final A"`.

Время сессии — **локальное время площадки** (Москва), таймзона в письме не указана; хранить наивным
datetime + отдельным полем `tz_name` (по умолчанию `None`). Не конвертировать по `Date` заголовка.

### 1.8. Ссылка отписки — идентификаторы

`https://primokarting.ru/results/?center=51&unsubscribe&client=10001&id=1000001&key=EXAMPLEKEY01`

* `center=51` — id клуба/площадки в Apex Timing → `club.external_id`;
* `client=10001` — id **получателя** в системе клуба → стабильный `driver.external_id` (ник может меняться);
* `id`, `key` — идентификаторы рассылки, хранить только в провенансе.

---

## 2. Формат времени

Все длительности хранить **целыми миллисекундами** (`int`), никаких float в БД и в API.

Поддерживаемые входные формы: `28.872`, `1:02.345`, `1'02.345`, `1:02,345`, `01:02.345`,
`1:02:03.456` (ч:м:с), `-`, `--`, пусто, `&nbsp;` → `None`.
Дробная часть может быть 1–3 знака (`28.8` → 28800 мс), нормализовать по длине, а не умножением на 1000.

Форматирование обратно: `< 60 s` → `28.872`; `>= 60 s` → `1:02.345`.

---

## 3. Доменная модель

Канонический контракт — `karting/models.py` (dataclasses, уже написан). Не менять поля без нужды;
если нужно поле — добавлять с дефолтом, чтобы не ломать смежные модули.

Ключевые сущности:

* `Club` — организатор (`external_id=51`, `name="PRIMO KARTING"`, домен).
* `Session` — гонка/сессия: `name`, `code`, `started_at` (наивный local), `track`, `category`, `club`.
* `Driver` — пилот: ник + опциональный `external_id` (для получателя письма).
* `SessionEntry` — участие пилота в сессии: `position`, `kart`, `laps_count`, `gap_ms`, `gap_laps`, `best_lap_ms`.
* `Lap` — круг: `driver`, `lap_number`, `time_ms` (nullable), `sectors: list[int|None]`, `is_best`.
* `RankingEntry` — строка рейтинга (`weekly_best` / `track_record`): `rank`, `driver`, `best_lap_ms`, `category`.
* `HistoryEntry` — строка «Your last sessions»: `date`, `position`, `best_lap_ms`, `laps_count`.
* `ParsedEmail` — всё вышеперечисленное + `provenance` (message_id, subject, sent_at, recipient, sha256 файла).

## 4. Требования к парсеру

1. **Ничего не терять и ничего не выдумывать.** Неизвестная таблица → в `ParsedEmail.unparsed`
   (сигнатура шапки + текст), а не молчаливый пропуск.
2. **Классификация таблиц по шапке**, а не по индексу таблицы в документе. Заголовки распознавать
   через словарь синонимов EN/FR/RU (`Rnk|Clt|Поз`, `Driver|Pilote|Пилот`, `Best lap|Meilleur tour|Лучший круг`, …),
   сравнение — casefold + схлопывание пробелов + ` `/`​` → обычный пробел.
3. **Валидация с явными предупреждениями** (`ParsedEmail.warnings: list[str]`, парсинг не падает):
   * число кругов в lap chart ≠ `Laps` из классификации;
   * `best_lap_ms` из классификации ≠ минимум по кругам пилота;
   * подсветка `#515151` не на минимальном круге;
   * `sum(sectors) != time_ms` (допуск 0 мс — Apex не округляет);
   * времена получателя из lap chart ≠ из личной таблицы;
   * пилот из подиума отсутствует в классификации.
4. **Строгий режим**: `parse(..., strict=True)` поднимает `ParseError` на любых warnings; по умолчанию — нет.
5. Работать с `.eml` (`email.message_from_binary_file`, `policy=default`) и с уже извлечённым HTML.
6. Никаких сетевых запросов при парсинге.

## 5. Хранилище

SQLite (`data/pace.db`), доступ через `sqlite3` из stdlib (без ORM), `PRAGMA foreign_keys=ON`,
`journal_mode=WAL`. Времена — `INTEGER` мс. Схема в `karting/storage/schema.sql`, применяется
идемпотентно при открытии соединения.

Обязательные свойства:

* **Идемпотентный импорт.** Повторный импорт того же письма не создаёт дублей.
  Ключ сессии — `(club_id, name, code, started_at)`, **не** message-id: одну и ту же гонку могут
  прислать несколько писем (разным получателям).
* **Слияние.** Второе письмо той же гонки добавляет только новое (свои сектора получателя),
  не затирая существующие данные. Расхождения по одинаковым ключам → запись в таблицу конфликтов/лог.
* **Провенанс.** Таблица `email_import` (message_id UNIQUE, sha256 файла, дата импорта, путь, статус,
  json warnings) + `raw_email` (сырой .eml или его копия в `data/raw_emails/`).
* **Ручная разметка кругов.** Отдельная таблица `lap_annotation`
  (`lap_id`, `tag`, `note`, `created_at`, `source='manual'`) — статистика умеет исключать/группировать
  по тегам. Тэги-справочник: `penalty`, `boost`, `pit`, `traffic`, `incident`, `outlier`, `invalid` (расширяемо).
  Данные в `lap` никогда не меняются импортом задним числом.
* Хранить и «сырое» время круга, и вычисляемые признаки — но **признаки не материализовать**, считать на лету.

## 6. Статистика (первый срез)

Модуль `karting/stats/`:

* Описательные метрики темпа по пилоту в сессии: `laps`, `clean_laps`, `best`, `median`, `mean`,
  `trimmed_mean` (10%), `std`, `iqr`, `mad`, `cv` (std/mean), `consistency = std / median`,
  `pace_delta_to_best_driver` (**по среднему**, не по медиане: суммарное время отрезка равно
  `n × mean`, поэтому разрыв средних напрямую переводится в потерянные секунды дистанции;
  решение владельца от 12.08.2026), `theoretical_best` (сумма лучших секторов, если сектора есть),
  `degradation` (наклон OLS «время круга ~ номер круга», мс/круг + p-value).
* Детекция выбросов (робастная): круг считается «грязным», если
  `time > median + k * MAD_scaled` (k=3 по умолчанию, `MAD_scaled = 1.4826 * MAD`) или `time is None`.
  Быстрые аномалии (`time < median - k*MAD`) помечаются **отдельным** флагом `suspicious_fast` и по
  умолчанию **не** выбрасываются — решение по ним за пользователем (см. §5 ручная разметка).
  Ни один фильтр не «зашит»: все функции принимают `LapFilter` (какие теги исключать, порог k, min laps).
* Сравнение двух пилотов: разность средних/медиан, Welch t-test, Mann–Whitney U, bootstrap-CI разности
  медиан (10k ресэмплов, seed фиксирован), Cliff's delta / Hedges' g, Levene/Brown–Forsythe на равенство
  дисперсий. Возвращать структуру с `statistic`, `p_value`, `ci`, `n1`, `n2`, `effect_size`, `interpretation`
  и **явной пометкой о зависимости наблюдений** (круги одной гонки не независимы: трафик, погода, топливо) —
  честная интерпретация важнее красивого p-value.
* Все функции чистые, на numpy/scipy, без обращения к БД.

## 7. API и дашборд

FastAPI (`karting/api/app.py`), JSON, CORS для dev-фронта.
Минимум эндпоинтов: список сессий, детали сессии (классификация + все круги), статистика сессии,
сравнение двух пилотов, история пилота, загрузка `.eml` (multipart) с возвратом отчёта об импорте,
теги кругов (CRUD для ручной разметки).

CLI (`karting/cli.py`): `import <paths...>`, `sessions`, `show <session_id>`, `export <session_id> --json`,
`serve`.

Фронтенд `web/` (Vite + React + TypeScript + Recharts): страница сессии — классификация,
график времён кругов по пилотам, распределения (box/violin-подобное), таблица метрик темпа,
блок сравнения двух пилотов со статтестами. Дизайн-система — по скиллу `dataviz`.

## 8. Интерфейсы модулей (жёсткий контракт)

Модули пишутся параллельно, поэтому сигнатуры ниже фиксированы. Реализация может добавлять
приватные хелперы и **дополнительные** необязательные аргументы с дефолтами, но не менять
перечисленные имена, порядок обязательных аргументов и форму возвращаемых структур.

### 8.1. `karting.parsing`

```python
from karting.models import ParsedEmail

def parse_email_file(path: str | Path, *, strict: bool = False) -> ParsedEmail: ...
def parse_email_bytes(raw: bytes, *, source_path: str | None = None, strict: bool = False) -> ParsedEmail: ...
def parse_html(html: str, *, strict: bool = False) -> ParsedEmail: ...   # без заголовков письма
```

`karting.parsing.timeparse`:

```python
def parse_duration(text: str | None) -> int | None:      # "28.872" -> 28872; "1:02.345" -> 62345; "-" -> None
def format_duration(ms: int | None) -> str:              # 28872 -> "28.872"; 62345 -> "1:02.345"; None -> "-"
def parse_gap(text: str | None) -> tuple[int | None, int | None]:   # -> (gap_ms, gap_laps)
```

### 8.2. `karting.storage`

```python
@dataclass(slots=True)
class ImportReport:
    session_id: int
    club_id: int
    session_created: bool
    already_imported: bool          # это же письмо (message_id/sha256) уже импортировали
    inserted_laps: int
    updated_laps: int               # дополнили секторами существующие круги
    inserted_entries: int
    conflicts: list[str]            # расхождения с уже сохранёнными данными
    warnings: list[str]             # проброшенные warnings парсера

def open_db(path: str | Path = "data/pace.db") -> Database: ...

class Database:
    def close(self) -> None: ...
    def import_parsed(self, parsed: ParsedEmail, *, raw_bytes: bytes | None = None) -> ImportReport: ...
    def list_sessions(self) -> list[dict]: ...
    def get_session(self, session_id: int) -> dict | None: ...     # session + club + entries
    def session_laps(self, session_id: int) -> list[dict]: ...     # + tags на каждом круге
    def list_drivers(self) -> list[dict]: ...
    def driver_history(self, nickname: str) -> list[dict]: ...
    def rankings(self, session_id: int) -> dict: ...               # {"weekly_best": [...], "track_record": [...]}
    def add_lap_tag(self, lap_id: int, tag: str, note: str | None = None) -> None: ...
    def remove_lap_tag(self, lap_id: int, tag: str) -> None: ...
    def lap_tags(self, session_id: int) -> dict[int, list[dict]]: ...
```

`Database` должен поддерживать `with open_db(...) as db:` и путь `":memory:"` (для тестов).

### 8.3. `karting.stats`

```python
@dataclass(slots=True, frozen=True)
class LapFilter:
    exclude_tags: frozenset[str] = frozenset({"penalty", "boost", "pit", "invalid", "outlier"})
    mad_k: float = 3.0
    drop_missing: bool = True
    drop_first_lap: bool = True     # первый круг — старт/выезд, не показатель темпа
    drop_slow_outliers: bool = True
    drop_fast_outliers: bool = False
    min_laps: int = 3

@dataclass(slots=True)
class LapPoint:                      # вход статистики, независим от БД
    lap_number: int
    time_ms: int | None
    sectors: tuple[int | None, ...] = ()
    tags: tuple[str, ...] = ()

@dataclass(slots=True)
class LapFlags:
    lap_number: int
    used: bool
    reason: str | None               # "missing" | "first_lap" | "tag:penalty" | "slow_outlier" | "fast_outlier"
    suspicious_fast: bool = False

def classify_laps(laps: Sequence[LapPoint], flt: LapFilter = LapFilter()) -> list[LapFlags]: ...

@dataclass(slots=True)
class PaceStats:
    n_laps: int; n_used: int
    best_ms: int | None; median_ms: float | None; mean_ms: float | None
    trimmed_mean_ms: float | None; std_ms: float | None; iqr_ms: float | None
    mad_ms: float | None; cv: float | None; consistency: float | None
    theoretical_best_ms: int | None
    degradation_ms_per_lap: float | None; degradation_p_value: float | None
    used_lap_numbers: list[int]; excluded: list[LapFlags]

def pace_stats(laps: Sequence[LapPoint], flt: LapFilter = LapFilter()) -> PaceStats: ...

@dataclass(slots=True)
class TestResult:
    name: str; statistic: float | None; p_value: float | None
    ci_low: float | None = None; ci_high: float | None = None
    effect_size: float | None = None; effect_name: str | None = None
    interpretation: str = ""

@dataclass(slots=True)
class DriverComparison:
    driver_a: str; driver_b: str
    stats_a: PaceStats; stats_b: PaceStats
    n_a: int; n_b: int
    mean_diff_ms: float | None; median_diff_ms: float | None
    tests: list[TestResult]          # Welch t, Mann-Whitney U, Levene, bootstrap median diff
    caveats: list[str]               # напр. про зависимость наблюдений и малый n

def compare_drivers(a: Sequence[LapPoint], b: Sequence[LapPoint], *, name_a: str = "A", name_b: str = "B",
                    flt: LapFilter = LapFilter(), n_boot: int = 10000, seed: int = 12345) -> DriverComparison: ...
```

Все dataclass-и статистики обязаны иметь `to_dict()` или быть сериализуемыми через `dataclasses.asdict`.

### 8.4. HTTP API

Базовый префикс `/api`. Все времена — целые мс в полях с суффиксом `_ms`.

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","sessions":<int>}` |
| GET | `/api/sessions` | `[{id,name,code,started_at,track,category,club,drivers_count,laps_count}]` |
| GET | `/api/sessions/{id}` | `{session,club,entries:[...],laps:[{id,driver,lap_number,time_ms,sectors,is_best,tags}]}` |
| GET | `/api/sessions/{id}/stats` | `{filter:{...},drivers:[{driver,position,...PaceStats}]}` |
| GET | `/api/sessions/{id}/compare?a=&b=` | `DriverComparison` |
| GET | `/api/sessions/{id}/rankings` | `{"weekly_best":[...],"track_record":[...]}` |
| GET | `/api/drivers` | `[{nickname,sessions_count,best_lap_ms,last_seen}]` |
| GET | `/api/drivers/{nickname}/history` | `[{date,position,best_lap_ms,laps_count,session_id}]` |
| POST | `/api/imports` | multipart `files=[]` (.eml) → `[ImportReport]` |
| GET | `/api/tags` | `[{value,label}]` — справочник тегов |
| POST | `/api/laps/{lap_id}/tags` | `{tag,note}` → `204` |
| DELETE | `/api/laps/{lap_id}/tags/{tag}` | `204` |

Query-параметры фильтра (общие для `/stats` и `/compare`): `mad_k`, `drop_first_lap`, `drop_slow_outliers`,
`drop_fast_outliers`, `exclude_tags` (csv), `min_laps`.
Ошибки — стандартный `{"detail": "..."}` с корректным HTTP-кодом (404 на неизвестную сессию/пилота,
422 на кривой запрос, 400 на нераспарсенный `.eml`).

## 9. Конвенции репозитория

* Python 3.12, единственное окружение — `/opt/workspace/pace-analysis/.venv` (уже создано, зависимости
  установлены). **Не создавать новых venv, не запускать `pip install` без крайней необходимости**
  (тогда — только `.venv/bin/pip`).
* Запуск: `.venv/bin/python -m pytest`, `.venv/bin/python -m karting.cli ...`.
* Типизация обязательна (аннотации на всех публичных функциях), докстринги — короткие, по делу.
* Комментарии, докстринги, имена и код — на английском.
* **Тексты интерфейса дашборда (`web/`) — на русском** (решение владельца от 11.08.2026): заголовки
  карточек, подписи, кнопки, тултипы, сообщения об ошибках и пустых состояниях. Вывод CLI и `detail`
  в ответах API пока остаются английскими — их перевод не делался, чтобы не переписывать ожидания
  424 тестов; если понадобится, это отдельная задача.
* Никаких «заглушек» и `TODO` в отданном коде: если функция объявлена — она работает.

---

## 10. Джокер и пит-стоп — доменное правило гонки

**Подтверждено владельцем данных 11.08.2026.** В этом формате гонки каждый пилот обязан один раз
за заезд проехать **джокер** (срезка — круг становится примерно на 1.9 с быстрее) и один раз
**заехать на пит-стоп** (круг становится примерно на 13 с медленнее). Оба круга **не являются темпом**
и по умолчанию исключаются из анализа. Аномалия каждый раз локализована **в одном секторе**:
у WLAD111 джокер — это S1 12.241 при обычных 13.9–14.2, пит — S2 26.343 при обычных ~14.0.

### 10.1. Почему нельзя детектировать через MAD

Проверено на эталонной гонке: порог «медиана ± 3·MAD» находит джокер безошибочно, но для пита даёт
ложные срабатывания (у KOLYA11 в кандидаты попадают обычные круги 29.558 и 28.276, у PHREEMAN — 29.349
и 29.536), потому что у стабильного пилота MAD мал. Разделение по **отношению к собственной робастной
базе** — чистое, с огромным запасом:

| | худший НЕ-пит круг | лучший пит-круг | лучший НЕ-джокер круг | худший джокер |
|---|---|---|---|---|
| ratio к медиане | 1.120 | 1.438 | 0.987 | 0.954 |

Отсюда дефолтные пороги: `pit_ratio = 1.25`, `joker_ratio = 0.97`.

### 10.2. Модуль `karting/stats/events.py`

```python
@dataclass(slots=True, frozen=True)
class EventDetectionConfig:
    pit_ratio: float = 1.25        # lap_time >= ratio * baseline -> кандидат в пит
    joker_ratio: float = 0.97      # lap_time <= ratio * baseline -> кандидат в джокер
    one_per_driver: bool = True    # ограничить ДЖОКЕР одним на пилота
    max_pits_per_driver: int | None = None   # None = сколько прошло порог (см. §10.5)
    require_single_sector: bool = True   # если сектора есть — аномалия должна быть заперта в одном секторе
    skip_first_lap: bool = True    # круг 1 (старт) не участвует ни в базе, ни в кандидатах

@dataclass(slots=True)
class DetectedEvent:
    driver: str
    lap_number: int
    kind: str                      # 'joker' | 'pit'
    ratio: float                   # lap_time / baseline
    delta_ms: int                  # lap_time - baseline
    sector_index: int | None       # номер аномального сектора, если подтверждено по секторам
    confidence: float              # 0..1
    note: str

@dataclass(slots=True)
class EventReport:
    events: list[DetectedEvent]
    drivers_without_joker: list[str]
    drivers_without_pit: list[str]
    drivers_with_multiple: list[str]
    warnings: list[str]

def detect_events(laps_by_driver: Mapping[str, Sequence[LapPoint]],
                  config: EventDetectionConfig = EventDetectionConfig()) -> EventReport: ...
```

База (`baseline`) — медиана кругов пилота без круга 1 и без уже найденных кандидатов
(двухпроходный расчёт: сначала грубая медиана, затем пересчёт по оставшимся кругам).

**Обязательная проверка на уровне сессии:** ожидается ровно один джокер и ровно один пит на пилота.
Отклонения не «чинятся» молча, а попадают в `EventReport` и далее в UI как приглашение к ручной разметке.

**Пит-стоп обязателен для каждого пилота ровно один раз** (подтверждено владельцем данных 11.08.2026),
поэтому `drivers_without_pit` — это не факт гонки, а сигнал о проблеме: либо сбой детекции, либо
неполные данные. Такой случай:
* попадает в `EventReport.warnings` с повышенной серьёзностью (в отличие от `drivers_without_joker`);
* обязан сопровождаться лучшим кандидатом — самым медленным кругом пилота с его `ratio` и `delta_ms`, —
  чтобы человек мог подтвердить пит одним действием, а не искать круг вручную.

Джокер таким жёстким инвариантом **не** является: его можно проехать с потерей времени и тогда он
статистически неотличим от обычного круга. Отсутствие джокера — мягкий сигнал, а не ошибка.
На эталонной гонке ожидаемый результат: 6 питов из 6 и 5 джокеров из 6 — у `ИГОРЬ53` джокер не
детектируется (его круг 15 = 32.178 идёт сразу после пита на круге 14, то есть джокер, судя по всему,
проехан с потерей). Это корректное поведение, а не баг: он должен попасть в `drivers_without_joker`.

### 10.2.1. Сколько питов в гонке — читается с поля, а не задаётся

**Найдено на реальных данных 11.08.2026.** В гонке `RACE DAY ENDURANCE` (10 пилотов, 983 круга) каждый
пилот заезжал на пит **дважды**. Правило «не больше одного пита на пилота» размечало только более
экстремальный из двух, второй оставался без тега, попадал в выборку темпа и проезжал горбом по
скользящему среднему шириной в окно сглаживания: размах кривой 8.161 с вместо 0.894 с.

Правило исправлено асимметрично, потому что события асимметричны по различимости:

* **Пит не ограничен по количеству** (`max_pits_per_driver = None`). Пит-круг отличим с огромным
  запасом: в эндюрансе 2.18–2.54 × базы против максимум 1.12 × у обычного круга, в спринте
  1.44 × против 1.12 ×. Считать их «сколько есть» безопасно.
* **Джокер по-прежнему один на пилота** (`one_per_driver = True`). Джокер быстрее базы всего на ~7%,
  и в эндюрансе на порог `0.97` проходят 2–4 круга у разных пилотов (слипстрим неотличим от срезки).
  Метить их все — значит выдумывать факты; лишние кандидаты уходят в `warnings`.

**Ожидаемое число питов определяется по согласию поля:** `EventReport.expected_pits` — мода
`pit_counts` среди пилотов, которые вообще заезжали. Пилот, чьё число расходится с полем, попадает в
`drivers_with_multiple` и в `warnings`; пилот без пита — в `drivers_without_pit` с предложением круга,
как и раньше. Детектор ничего не подгоняет под ожидание — он его сообщает.

Регрессия закреплена в `tests/test_events.py::TestTwoStopEnduranceRace` и
`TestPitCountConsensus`; поведение спринтовой `Final A` не изменилось (6 питов, 5 джокеров).

### 10.3. Хранение и приоритет разметки

* `lap_annotation` получает колонку `source` со значениями `'manual' | 'auto'`, UNIQUE(lap_id, tag, source).
* **Правило приоритета:** если у круга есть хоть одна ручная пометка — автоматические для этого круга
  игнорируются целиком. Ручное решение человека всегда главнее детектора.
* `Database.detect_and_tag_events(session_id, config)` пересчитывает автоматику: удаляет для сессии
  строки с `source='auto'` и вставляет заново. Строки `source='manual'` не трогает никогда.
* Автотегирование запускается при импорте сессии и доступно отдельной командой CLI и эндпоинтом API.

### 10.4. Влияние на статистику и на дашборд

* `LapFilter.exclude_tags` по умолчанию включает `joker` и `pit` (плюс `penalty`, `invalid`, `outlier`).
* **График «Динамика по ходу гонки» фильтрует дважды.** Разметка ловит известные события, но она не
  может быть полной: круг за сейфти-картом, разворот, второй пит, который детектор не увидел из-за
  порога, — всё это остаётся в данных. Поэтому перед усреднением из выборки выбрасываются и
  размеченные круги, и всё, что вышло за робастный диапазон пилота (медиана ± k · MAD, тот же `mad_k`,
  что и в общем фильтре страницы). Один круг на +35 с при окне 5 поднимает среднее на 7 с и тянется
  горбом пять кругов — сглаживание без фильтра показывает не темп, а инцидент. Число выброшенных
  кругов подписывается под графиком с разделением «по разметке / по статистике».
* `drop_fast_outliers` остаётся `False`: быстрые круги теперь отсекаются осмысленным тегом `joker`,
  а не слепым статистическим порогом. Флаг `suspicious_fast` сохраняется как сигнал «проверь глазами».
* **Официальный «Best lap» из письма — это джокер-круг у 5 пилотов из 6, и как метрика темпа он врёт.**
  CLI обязан показывать рядом две величины: `best lap (official)` из классификации и `best clean lap`
  из отфильтрованных кругов, с дельтой между ними и с рангами по обеим (`#OFF` и `#PACE`).
  В дашборде отдельная секция «Official vs clean best lap» **удалена по решению владельца 11.08.2026**
  как избыточная; официальный круг остаётся виден в карточке «Классификация», чистый — в «Метриках
  темпа», и подпись последней явно предупреждает, что это разные величины. Если прямое сопоставление
  снова понадобится — это две колонки в таблице метрик, а не отдельная секция. На эталонной гонке порядок пилотов
  по этим двум метрикам разный: по официальному WLAD111 пятый (26.788), по чистому — второй (27.804),
  в 13 мс от лидера TWG (27.791). Это ключевая ценность продукта, а не декоративная деталь.
* То же предупреждение относится к таблицам «Best times of the week» и «Track records» из письма:
  они построены на официальных лучших кругах и, вероятно, тоже содержат джокеры. Не выдавать их
  за темп; помечать в UI как «official, joker-inflated».

---

## 11. Обезличивание эталонного письма

Репозиторий публичный, поэтому `primo-karting-final-a.eml` и производные от него файлы содержат
**фиктивные** идентификаторы получателя. Замена сделана 13.08.2026 согласованно во всех местах,
поэтому golden-тест проходит на них так же, как проходил на оригинале.

| Поле | В репозитории | Что это было |
|---|---|---|
| Адрес получателя | `driver@example.com` | личная почта владельца |
| `key=` в ссылке отписки | `EXAMPLEKEY01` | рабочий токен отписки |
| `client=` (он же `driver.external_id`) | `10001` | id аккаунта в системе клуба |
| `id=` рассылки, `Apex-Id` | `1000001` | id письма в рассылке |
| Пилот на 51-й строке рекордов | `KARTMAN51` | реальные имя и фамилия третьего лица |

**Правила при добавлении новых писем в репозиторий:**

1. Прогнать те же замены **до** коммита. Проверять не только сырой файл, но и **декодированное**
   тело: quoted-printable переносит длинные строки через `=\n`, и токен может оказаться разорван,
   так что `grep` по сырому файлу способен пропустить вхождение.
2. Настоящие письма с личными данными складывать в `data/raw_emails/`, который целиком в `.gitignore` —
   импорт кладёт их туда сам.
3. Ники пилотов (`WLAD111`, `KOLYA11`, `ИГОРЬ53` и прочие) оставлены как есть: это псевдонимы,
   открыто опубликованные клубом в результатах и таблицах рекордов. А вот полные имена третьих лиц
   обезличивать обязательно.
4. Подписи DKIM/ARC в письме после подмены заголовков недействительны. Ничего в коде их не проверяет,
   но не стоит выдавать этот файл за криптографически подлинный образец.
