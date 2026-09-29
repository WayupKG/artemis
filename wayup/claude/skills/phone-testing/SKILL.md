---
name: phone-testing
description: Тестирование мобильного приложения на Android-телефоне через MCP artemis (форк WayupKG/artemis). Используй, когда просят проверить, прогнать, протестировать сценарий на телефоне, снять экран приложения, воспроизвести баг на устройстве или записать/запустить YAML-сценарий.
---

# Тестирование на телефоне через artemis

## Перед началом
1. `current_app` — какое приложение сверху, какой телефон, можно ли вводить (`input_allowed`).
   - `foreground: null` — экран заблокирован. Попроси разблокировать, PIN не вводи.
   - `profile: null` — в проекте нет `.artemis.json`. Предложи создать:
     `{"allowed_packages": ["<пакет приложения>"], "device": "<serial из list_devices>"}`.
     Без него защита выключена, а телефон может быть личным.
2. Если в проекте есть сценарии (обычно `phone-scenarios/*.yaml`) и просят «прогнать» —
   сразу `run_scenario`, без ручных шагов.

## Ручное исследование
- Действия (`tap_text`, `tap`, `input_into`, `back`, `scroll`, `launch_app` …) сами
  возвращают экран после того, как он успокоился. **Не вызывай `get_ui_hierarchy` после них.**
- Нажимай по тексту: `tap_text("Просрочено")`, вводи по подписи поля:
  `input_into("Найти задачу…", "Созвон")`. Координаты — только если у элемента нет текста.
- Строка экрана: `[x,y] Class "подпись" флаги`; флаги `tap input scroll selected disabled …`.
- Ждать загрузку — `wait_for("текст", timeout_ms=…)`, а не паузы.
- Скриншот — только когда важен внешний вид (цвета, вёрстка): `take_screenshot`.
  Для отчёта по багу — `take_screenshot(save_path=…)` и приложи файл к задаче.
- Отказ «refused, foreground app is …» — сверху чужое приложение. Не обходи защиту,
  запусти разрешённое приложение через `launch_app`.

## Сценарий после исследования
Когда путь пройден вручную, запиши его в YAML, чтобы следующий прогон шёл без модели:

```yaml
name: Просроченные задачи с главной
app: kg.replai.revision
steps:
  - launch: true
  - tap: Главная
  - tap: Просрочено
  - expect: "Просроченные, 7"          # запятая внутри — бери в кавычки
  - tap: Push-уведомления о назначениях
  - expect: [В работе, Критический]
  - screenshot: mob3-card
  - back: true
```

Шаги: `launch`, `stop`, `tap` (текст, `{text, nth}` или `[x, y]`), `long_press`,
`input: {field, text, clear}`, `expect` (ждёт до 3 с), `expect_not`,
`wait: {text, timeout_ms, gone}`, `scroll: down|up|left|right`, `key`, `back`,
`open_link`, `screenshot`, `sleep_ms`.

Запуск: `run_scenario(["phone-scenarios"])` или из терминала
`~/tools/artemis/.venv/bin/python -m wayup.scenario phone-scenarios` (код выхода 1 при падении).
Результаты и скриншоты падений — в `.artemis/runs/` (добавь `.artemis/` в `.gitignore`).

## Что не делать
- Не трогай данные без согласия: создание, удаление, отправка — только если просили.
- Не вводи пароли и PIN владельца.
