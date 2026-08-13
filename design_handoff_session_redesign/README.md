# Handoff: экран сессии Pace Analysis (редизайн)

## Overview

Пересобранная страница сессии для `wlad111/pace-analysis`: список пилотов по темпу с выбором
пары A / B, короткое статистическое сравнение справа, график скользящего среднего по ходу гонки
и большая таблица метрик. Экран отвечает на три вопроса, ради которых открывают дашборд: кто
реально быстрее по темпу, насколько пилот стабилен и где теряет, и как он выглядит против
конкретного соперника.

## About the design files

`prototype/Pace Analysis — Session Redesign v3.dc.html` — **дизайн-референс**, а не продакшн-код.
Это самостоятельная HTML-страница: откройте её в браузере (рядом должен лежать `support.js` из той
же папки), чтобы увидеть точные пропорции, состояния и поведение. Логика внутри неё считает
статистику в браузере только потому, что у прототипа нет бэкенда — **в приложении всё это уже
отдаёт API** (`/stats`, `/compare`), и повторять расчёты на фронте не нужно.

В `src/` лежит перенос дизайна в ваш стек: React 19 + TypeScript + Vite, ваши токены из
`web/src/styles.css`, ваши типы из `web/src/api.ts`, ваши хелперы из `web/src/format.ts` и
`web/src/metrics.ts`. Это рабочая отправная точка, а не библиотека: правьте под себя.

## Fidelity

**Hi-fi.** Цвета, размеры, интервалы и состояния окончательные и взяты из вашего же токен-набора.
Единственное добавление — переменная `--mono` для времён.

## Данные прототипа

Прототип считает по всем 120 кругам эталонной Final A (`tests/fixtures/final_a_expected.json` через
`web/src/mock/session.json`). Чистые лучшие круги совпадают с вашими до миллисекунды. Медианы в
прототипе могут отличаться от README на 15–25 мс: там «база» — двухпроходная робастная медиана по
всем кругам, здесь — медиана уже отфильтрованной выборки. В приложении этого расхождения не будет:
числа придут из `/stats`.

## Файлы

```
prototype/
  Pace Analysis — Session Redesign v3.dc.html   дизайн-референс, открывается в браузере
  support.js                                    рантайм прототипа (не нужен в приложении)
src/
  components/PaceLadder.tsx          список пилотов с выбором A / B
  components/PaceProgressChart.tsx   скользящее среднее, чистый SVG
  components/PaceMetricsTable.tsx    таблица метрик + колонка распределения
  components/CompareSummary.tsx      короткое сравнение A / B
  styles.redesign.css                новые классы, поверх ваших токенов
```

## Интеграция

1. **CSS.** Скопируйте `src/styles.redesign.css` в `web/src/` и добавьте
   `@import './styles.redesign.css';` в конец `web/src/styles.css` (или просто вклейте содержимое).
   Новых зависимостей нет, Recharts этим компонентам не нужен.
2. **Компоненты.** Скопируйте четыре файла в `web/src/components/`. Импорты уже настроены на ваши
   модули: `../api`, `../format`, `../metrics`, `../theme`, `./Card`.
3. **Card.** Компоненты используют ваш `Card` (`title`, `caption`, `actions`, `stale`) и `ViewToggle`
   для переключателей — оба уже есть в `web/src/components/Card.tsx`. `ViewToggle` типизирован как
   `<T extends string>`, поэтому окно графика передаётся строкой и приводится к числу в `onChange`.
4. **Состояние в `App.tsx`.** Добавьте три состояния и подключите блоки:

```tsx
const [ladderMetric, setLadderMetric] = useState<LadderMetric>('mean')
const [window, setWindow] = useState(5)
const [relative, setRelative] = useState(false)
// pair уже есть: pair.a — это A, pair.b — B.
const pickRival = useCallback(
  (driver: string) => {
    setPair((current) => (current === null || driver === current.a ? current : { ...current, b: driver }))
  },
  [],
)

<div className="session-grid">
  <PaceLadder
    rows={stats.data?.drivers ?? []}
    subject={pair?.a ?? ''}
    rival={pair?.b ?? ''}
    metric={ladderMetric}
    onMetric={setLadderMetric}
    onPick={pickRival}
    stale={stats.loading}
  />
  <CompareSummary comparison={comparison.data} error={comparison.error} stale={comparison.loading} />
  <div className="full">
    <PaceProgressChart
      laps={laps}
      usedLaps={usedLaps}
      subject={pair?.a ?? ''}
      rival={pair?.b ?? ''}
      window={window}
      onWindow={setWindow}
      relative={relative}
      onRelative={setRelative}
      mode={theme.mode}
      stale={detail.loading}
    />
  </div>
  <div className="full">
    <PaceMetricsTable
      rows={stats.data?.drivers ?? []}
      laps={laps}
      usedLaps={usedLaps}
      subject={pair?.a ?? ''}
      rival={pair?.b ?? ''}
      onPick={pickRival}
      mode={theme.mode}
      stale={stats.loading}
    />
  </div>
</div>
```

5. **Что убрать со страницы.** `ClassificationCard` (протокол уходит в раскрывающийся блок или на
   отдельную вкладку — его «Best lap» это джокер и на первом экране он дезинформирует),
   `PaceTable` (заменён `PaceMetricsTable`), `ComparePanel` (заменён `CompareSummary`),
   `PaceExplorer` (график кругов и распределения теперь живут в `PaceProgressChart` и в колонке
   «Распределение»). `ImportPanel` и список сессий — в сайдбар или в шапку: это навигация, а не
   контент. `EventsPanel` оставьте, но ниже — это рабочий инструмент разметки, а не первый экран.

## Откуда какие данные

| Компонент | Источник |
| --- | --- |
| `PaceLadder` | `StatsResponse.drivers`: `mean_ms`, `median_ms`, `std_ms`, `driver` |
| `PaceMetricsTable` | `StatsResponse.drivers` + `SessionDetail.laps`, отфильтрованные `used_lap_numbers` через ваш `usedLapsByDriver` |
| `PaceProgressChart` | `SessionDetail.laps` + `usedLaps`; скользящее среднее — ваш `rollingMean` из `metrics.ts` |
| `CompareSummary` | `DriverComparison`: `mean_diff_ms`, `median_diff_ms`, `tests[]`, `caveats[]` |

Тесты в `CompareSummary` ищутся по имени (`/welch/i`, `/mann/i`, `/bootstrap|median/i`, эффект по
`effect_name`), а всё, что панель не узнала, всё равно печатается списком — новый тест в
`karting.stats` появится на экране без правок фронта. Если имена в `TestResult.name` отличаются,
поправьте регулярки в `findTest`.

## Как отдать это Claude Code

Положите папку `design_handoff_session_redesign/` в корень репозитория и скажите примерно так:

> Прочитай `design_handoff_session_redesign/README.md` и внедри редизайн экрана сессии в `web/`.
> Компоненты из `design_handoff_session_redesign/src/components/` перенеси в `web/src/components/`,
> CSS подключи к `web/src/styles.css`, перестрой `web/src/App.tsx` по разделу «Интеграция».
> Ничего не считай на фронте — бери числа из `/stats` и `/compare`. Прогони `npm run build` и
> `make test`, покажи диф.

Прототип открывать не обязательно, но полезно: `prototype/…dc.html` открывается в браузере как
обычный файл и показывает интерактив (выбор B, окно графика, сортировки).

## Ключевые дизайн-решения (и почему)

1. **Список по темпу вместо классификации.** Первым идёт число, ради которого существует проект, а
   не официальный best lap. Порядок задаёт переключатель «по среднему / по медиане».
2. **Стабильность видна, а не читается.** В строке — полоса отставания и интервал «медиана ± SD» на
   общей для всех шкале: короткая линия = ровный пилот. В таблице то же самое подробнее: круги,
   IQR-бокс, медиана, ромб среднего.
3. **Скользящее среднее — главный график.** Окно 3 / 5 / 7 кругов, скользящее назад: точка над
   кругом N отвечает «каким был темп на последних N кругах». Джокер и пит не входят в среднее.
   Режим «к своей медиане» выравнивает пилотов по форме, а не по абсолютному темпу.
4. **Два цвета вместо шести.** A — `--accent`, B — `--series-2`, остальные — `deemphasis`. Ваши
   восемь CVD-безопасных слотов остаются для архива и общих графиков, но на экране «я против него»
   шесть равноправных хюэ мешают.
5. **Подписи серий на концах линий вместо легенды.** Глаз не ходит к легенде и обратно; подписи
   раздвигаются по вертикали (минимум 14 px) перед отрисовкой, иначе на плотном финише они сливаются.
6. **Времена моноширинные и крупные.** Числа — контент этой страницы: `--mono`, `tabular-nums`, и
   одна доминирующая колонка (среднее, 17–21 px) вместо десяти одинаковых 12 px.
7. **Знаковый ноль в тренде не печатается.** Наклон меньше точности отображения — это `±0.00`
   серым, а не «+0.00»: иначе таблица показывает результат там, где его нет.

## Design tokens

Все из вашего `styles.css`, кроме `--mono`.

| Роль | Светлая | Тёмная |
| --- | --- | --- |
| Поверхность карточки | `#fcfcfb` | `#1a1a19` |
| Фон страницы | `#f9f9f7` | `#0d0d0d` |
| Текст | `#0b0b0b` / `#52514e` / `#898781` | `#ffffff` / `#c3c2b7` / `#898781` |
| Сетка / ось | `#e1e0d9` / `#c3c2b7` | `#2c2c2a` / `#383835` |
| A (акцент) | `#2a78d6` | `#3987e5` |
| B | `#eb6834` (`--series-2`) | `#d95926` |
| Хорошо / плохо | `#0ca30c` / `#d03b3b` | те же |

Типографика: интерфейс — `system-ui`; времена — `--mono`
(`ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace`), всегда с
`font-variant-numeric: tabular-nums`. Размеры: 34 px — разность в сравнении, 21 px — метрика в
списке, 17 px — среднее в таблице, 15 px — заголовки карточек, 13–14 px — текст, 11–12 px — подписи.
Радиусы `--radius` 10 px и `--radius-sm` 6 px, отступ карточки 14/16 px, `--gap` 16 px.

## Что осталось за кадром

- **Архив сессий.** Экран по-прежнему сессионно-центричный. Списки, фильтры по клубу и трассе,
  сравнение пилота между гонками — это следующий шаг, и он же чинит главную оговорку статистики
  (одна гонка не даёт вариативности).
- **Мобильная версия.** Специально не делал по вашему решению; `session-grid` схлопывается в одну
  колонку на &lt; 1180 px, но телефонный сценарий стоит проектировать отдельно.
- **Разметка кругов.** `EventsPanel` оставлен как есть; в прототипе v1 (в проекте) есть вариант,
  где подтверждение джокера и пита живёт прямо на графике — если захотите, перенесём.
