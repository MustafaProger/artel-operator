#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"

port=8790
url="http://127.0.0.1:${port}/"

if lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
  if curl --fail --silent --show-error --max-time 5 "${url}" >/dev/null; then
    echo "Артель Оператор уже запущен: ${url}"
    exit 0
  fi

  echo "Порт ${port} уже занят другим или неработоспособным процессом."
  echo "Освободите порт или остановите этот процесс, затем запустите файл снова."
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo 'Создаю виртуальное окружение...'
  python3 -m venv .venv
fi

echo 'Устанавливаю зависимости...'
.venv/bin/python -m pip install --requirement requirements.txt

if ! .venv/bin/python -m playwright install --dry-run chromium >/dev/null 2>&1; then
  echo 'Устанавливаю Chromium для автоматизации...'
  .venv/bin/python -m playwright install chromium
fi

echo "Запускаю Артель Оператор: ${url}"
exec .venv/bin/python -m uvicorn operator_app.main:app --host 127.0.0.1 --port 8790 --workers 1
