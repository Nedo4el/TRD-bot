# Образ на основе официального Python 3.11 (лёгкий вариант - slim)
FROM python:3.11-slim

# Отключаем генерацию __pycache__ внутри контейнера
ENV PYTHONDONTWRITEBYTECODE=1
# Вывод логов без буферизации (важно для docker logs)
ENV PYTHONUNBUFFERED=1

# Рабочая директория приложения внутри контейнера
WORKDIR /app

# Сначала копируем только requirements — чтобы Docker кешировал слой
# с зависимостями и не переустанавливал их при каждом изменении кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем весь код проекта
COPY . .

# Папки для логов и данных (монтируются из docker-compose)
RUN mkdir -p /app/logs /app/data

# Запуск торгового бота
CMD ["python", "main.py"]