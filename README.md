# MiniDB — собственная файловая мини-СУБД

Лабораторная работа №1 по базам данных. Всё написано на чистом Python 3.8+ без внешних библиотек и без готовых СУБД (SQLite, pandas, ORM, JSON-хранилищ не используется). Данные хранятся в собственном бинарном формате.

## Быстрый старт

```bash
python main.py            # база создаётся в папке data/
minidb> help              # список команд
minidb> seed              # 5 datasets, 5 models, 20 experiments
```

Файлы проекта:

| Файл | Назначение |
|---|---|
| `minidb.py` | движок: хранение, CRUD, ключи, индекс, JOIN, backup/restore |
| `main.py` | командная строка, генератор данных, benchmark |
| `schema.dbml` | схема для [dbdiagram.io](https://dbdiagram.io/d) |
| `data/` | файлы хранилища с тестовыми данными (создаются командой `seed`) |

## Схема базы

| Таблица | Поля |
|---|---|
| `datasets` | `id` (PK), `name`, `task`, `size` |
| `models` | `id` (PK), `name`, `version` |
| `experiments` | `id` (PK), `dataset_id` (FK → datasets.id), `model_id` (FK → models.id), `accuracy`, `status` |

Типы: `INT` (8 байт), `FLOAT` (8 байт), `STRING` (64 байта). Схему для отчёта получить так: вставить `schema.dbml` на dbdiagram.io и сделать скриншот.

## Архитектура

Два слоя:

1. **Движок (`minidb.py`)**: классы `DB` (папка с таблицами) и `Table` (одна таблица). Ничего не знает про ввод-вывод. Ошибки выбрасывает как `DBError`.
2. **Интерфейс (`main.py`)**: разбирает команды (`shlex`), вызывает движок, печатает результат, ловит `DBError`, поэтому программа не падает на ошибках пользователя.

## Формат хранения

Каждая таблица состоит из двух файлов в папке базы:

* `<table>.meta` — схема, обычный текст: по строке на столбец (`имя ТИП [pk] [fk=таблица.поле]`).
* `<table>.dat` — бинарные записи **фиксированной длины**: 1 байт флага «жива/удалена» + поля подряд (`struct`, формат `<?` + `q` для INT, `d` для FLOAT, `64s` для STRING).

Размер записи: `datasets` — 145 байт, `models` — 137, `experiments` — 97 (1 + 8 + 8 + 8 + 8 + 64). Запись номер `k` лежит со смещения `k × размер`.

* **INSERT** — дописывание в конец файла.
* **UPDATE** — перезапись записи на месте (`seek` + `write`).
* **DELETE** — запись помечается удалённой (нулевой флаг), физически остаётся в файле.
* **SELECT** — просмотр файла запись за записью (Seq Scan) либо поиск через индекс.

Данные переживают перезапуск: состояние живёт только в файлах, при запуске схемы читаются из `.meta`.

## Реализованные функции

* `CREATE TABLE`: поля, типы `INT/FLOAT/STRING`, `PRIMARY KEY`, `FOREIGN KEY`.
* CRUD: `insert`, `select`, `update`, `delete`.
* Авто-ID (максимум + 1), запрет дубликатов PK, запрет изменения PK.
* Проверка типов и длины строки; проверка ссылочной целостности:
  нельзя добавить/изменить запись с несуществующим внешним ключом и нельзя удалить запись, на которую есть ссылки.
* `select`: выбор столбцов, фильтры `= != > < >= <=`, условия `and` / `or` (`and` сильнее `or`).
* `INNER JOIN` нескольких таблиц.
* **Индекс `id → offset`** по первичному ключу (в памяти, строится при первом обращении, поддерживается при insert/delete). Команда `select` показывает «план»: `Seq Scan` или `Index Scan по id`.
* **Benchmark** поиска до/после индекса (`bench`), **генератор** больших данных (`gen N`).
* **Backup/restore** (`backup file.zip`, `restore file.zip`).

## Команды

```
seed                                    тестовые данные (5 datasets, 5 models, 20 experiments)
gen <N>                                 добавить N случайных experiments
bench [K]                               сравнить поиск по id без индекса и с индексом
backup <файл.zip> / restore <файл.zip>  резервная копия / восстановление
tables
create <таблица> id:INT:pk name:STRING dataset_id:INT:fk=datasets.id ...
insert <таблица> col=val col=val ...    (PK можно не указывать)
select <таблица> [col1,col2] [where ...]
select <таблица> join <т2> on <т1.поле> = <т2.поле> [join ...] [т.поле,т.поле] [where ...]
update <таблица> set col=val ... [where ...]
delete <таблица> [where ...]
exit
```

Условие: `where col op val [and col op val] [or ...]`. Значения с пробелами берутся в кавычки: `name="Hello World"`. Вокруг `=` в `join ... on` нужны пробелы.

Примеры:

```
select experiments id,accuracy where accuracy >= 0.9 and status = completed
select experiments where status = failed or accuracy < 0.7
select experiments join datasets on experiments.dataset_id = datasets.id join models on experiments.model_id = models.id experiments.id,datasets.name,models.name,experiments.accuracy where experiments.accuracy > 0.95
update experiments set status=completed where id = 3
delete experiments where id = 21
```

## Сценарий демонстрации

1. `python main.py`, затем `seed`, `tables`, `select datasets`.
2. `select experiments id,accuracy where accuracy >= 0.9 and status = completed`.
3. `insert experiments dataset_id=1 model_id=2 accuracy=0.5 status=running` (id выдаётся автоматически).
4. `update experiments set status=completed where id = 21`, `delete experiments where id = 21`.
5. Ошибки: `insert experiments dataset_id=99 model_id=1 accuracy=0.5 status=x` (неверный FK), `delete datasets where id = 1` (есть ссылки), `insert experiments id=1 ...` (дубликат).
6. JOIN: пример выше.
7. `exit`, запустить снова, `select experiments`: данные на месте.
8. Индекс: `select experiments where id = 3` показывает `план: Index Scan по id`, `select experiments where status = failed` — `Seq Scan`.
9. `gen 5000`, `bench 300`: сравнение скорости.
10. `backup copy.zip`, `delete experiments where id <= 10`, `restore copy.zip`, `select experiments where id = 5`: данные восстановлены.

## Benchmark индекса

Поиск по `id` среди 5020 записей, 300 запросов (`gen 5000`, затем `bench 300`). Пример запуска, у вас цифры будут другими:

| Режим | Всего, мс | На один поиск, мс |
|---|---|---|
| без индекса (Seq Scan) | 3064.57 | 10.215 |
| с индексом (Index Scan) | 2.83 | 0.009 |

Индекс строится один раз за 7.58 мс. Ускорение около ×1000, и оно растёт с размером таблицы: Seq Scan читает весь файл (O(n)), индекс делает один `seek` (O(1)).

## Воспроизводимость

* Зависимостей нет, нужен только Python 3.8+.
* `seed` и `gen` используют фиксированные значения `random.seed`, поэтому данные одинаковы при каждом запуске.
* Чистый запуск: удалить папку `data/`, выполнить `python main.py`, затем `seed`.

## Ограничения

* Нет транзакций и журнала: при сбое питания во время записи файл может повредиться.
* Нет блокировок: одновременная работа нескольких процессов не поддерживается.
* Строка STRING — не длиннее 64 байт (кириллица занимает 2 байта на букву).
* Удалённые записи остаются в файле; ID последней удалённой записи может быть выдан снова.
* Индекс только по первичному ключу и только для условия `id = значение`; в памяти, при запуске строится заново.
