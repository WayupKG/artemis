#!/usr/bin/env bash
# Pull Google's latest main into our fork and check that wayup still works.
# Our code lives only in wayup/ (plus the sync workflow), so merges are clean
# unless upstream renames something wayup uses — the tests below catch that.
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch upstream
if git merge-base --is-ancestor upstream/main HEAD; then
  echo "Уже актуально: upstream/main влит."
  exit 0
fi

git merge --no-edit upstream/main
uv sync --dev --locked || echo "uv sync не прошёл — зависимости могли устареть"
.venv/bin/python -m pytest wayup/tests -q -p no:cacheprovider
.venv/bin/python -c "import asyncio; from wayup import server; print(len(asyncio.run(server.mcp.list_tools())), 'инструментов')"
echo "Готово. Проверь и отправь: git push origin main"
