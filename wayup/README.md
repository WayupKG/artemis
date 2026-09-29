# wayup — наш слой поверх Google ARTEMIS

Быстрый и удобный для Claude Code MCP-сервер для Android-телефона.
Весь наш код лежит в `wayup/` (и `.github/workflows/wayup-sync.yml`), файлы Google
не правим, поэтому их обновления вливаются без конфликтов.

## Что даёт по сравнению с `artemis.mcp.adb_server`

| | Google | wayup |
|---|---|---|
| Дерево экрана | ~1000 мс, JSON ~70 тыс. символов | ~150 мс, ~1,5 тыс. символов, строка на элемент |
| После действия | «Success», экран — отдельным вызовом | экран после стабилизации в том же ответе |
| Скриншот | base64 текстом, ~1000 мс | картинка, ~400 мс |
| Фокус поля перед вводом | ~2 с (дерево + пауза 1 с) | ожидание по факту |
| Нажать/ввести по тексту | нет | `tap_text`, `input_into`, `wait_for` |
| Сценарии без модели | нет | YAML + `run_scenario` / `python -m wayup.scenario` |
| Защита личного телефона | нет | `.artemis.json` → `allowed_packages` |
| Несколько устройств | через env | `list_devices`, `select_device`, `device` в профиле |

## Подключение к Claude Code

```sh
claude mcp add artemis --scope user \
  -e PYTHONUNBUFFERED=1 -e PYTHONPATH=$HOME/tools/artemis -e ARTEMIS_KEEP_DEVICE_AWAKE=false \
  -- $HOME/tools/artemis/.venv/bin/python -m wayup.server
ln -s $HOME/tools/artemis/wayup/claude/skills/phone-testing ~/.claude/skills/phone-testing
```

## Профиль проекта `.artemis.json`

Ищется вверх от папки, где запущен Claude Code (или `ARTEMIS_PROFILE`):

```json
{
  "allowed_packages": ["kg.replai.revision"],
  "device": "10AG3Z32G6001L4",
  "show_system_ui": false
}
```

## Сценарии

Формат — в `wayup/scenario.py` и в skill `phone-testing`. Запуск из терминала:

```sh
~/tools/artemis/.venv/bin/python -m wayup.scenario phone-scenarios/
```

## Обновления от Google

- Автоматически: `wayup-sync.yml` раз в неделю вливает `google/artemis` в ветку
  `sync/upstream-ДАТА`, гоняет тесты wayup и открывает PR (или issue при конфликте).
- Вручную: `wayup/sync_upstream.sh`.

## Тесты

```sh
.venv/bin/python -m pytest wayup/tests -q
```
