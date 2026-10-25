"""CLI для MiniDB. Запуск: python main.py [папка_базы]"""
import random
import shlex
import sys
import time

from minidb import DB, DBError

HELP = """Команды:
  seed                                    тестовые данные (5 datasets, 5 models, 20 experiments)
  gen <N>                                 добавить N случайных experiments (для benchmark)
  bench [K]                               сравнить поиск по id без индекса и с индексом (K поисков)
  backup <файл.zip> / restore <файл.zip>  резервная копия / восстановление
  tables
  create <таблица> id:INT:pk name:STRING dataset_id:INT:fk=datasets.id ...
  insert <таблица> col=val col=val ...    (PK можно не указывать - будет авто-ID)
  select <таблица> [col1,col2] [where ...]
  select <таблица> join <т2> on <т1.поле> = <т2.поле> [join ...] [т.поле,т.поле] [where ...]
  update <таблица> set col=val ... [where ...]
  delete <таблица> [where ...]
  exit
where: col op val [and col op val] [or ...]   операторы: = != > < >= <=   (and сильнее or)"""

STATUSES = ["completed", "running", "failed"]


def seed(db):
    if db.tables:
        raise DBError("база уже содержит таблицы")
    db.create_table("datasets", ["id INT pk", "name STRING", "task STRING", "size INT"])
    db.create_table("models", ["id INT pk", "name STRING", "version STRING"])
    db.create_table("experiments", ["id INT pk", "dataset_id INT fk=datasets.id",
                                    "model_id INT fk=models.id", "accuracy FLOAT", "status STRING"])
    for n, t, s in [("MNIST", "classification", 70000), ("CIFAR-10", "classification", 60000),
                    ("IMDB", "sentiment", 50000), ("Iris", "classification", 150),
                    ("Titanic", "classification", 891)]:
        db.table("datasets").insert(name=n, task=t, size=s)
    for n, v in [("LogReg", "1.0"), ("RandomForest", "1.2"), ("SVM", "2.0"), ("CNN", "3.1"), ("BERT", "4.0")]:
        db.table("models").insert(name=n, version=v)
    random.seed(1)
    for _ in range(20):
        db.table("experiments").insert(dataset_id=random.randint(1, 5), model_id=random.randint(1, 5),
                                       accuracy=round(random.uniform(0.6, 0.99), 2),
                                       status=random.choice(STATUSES))


def gen(db, n):
    """Генератор больших данных: n случайных experiments (проходят все проверки, включая FK)."""
    exp = db.table("experiments")
    ds = [r["id"] for r in db.table("datasets").select(["id"])]
    ms = [r["id"] for r in db.table("models").select(["id"])]
    if not ds or not ms:
        raise DBError("сначала выполните seed")
    random.seed(7)
    start = max((r["id"] for r in exp.select(["id"])), default=0)
    for i in range(1, n + 1):
        exp.insert(id=start + i, dataset_id=random.choice(ds), model_id=random.choice(ms),
                   accuracy=round(random.uniform(0.5, 0.99), 2), status=random.choice(STATUSES))
    print(f"добавлено {n}, всего experiments: {len(exp.select(['id']))}")


def bench(db, k=300):
    """Поиск по id: полный просмотр файла (Seq Scan) против индекса id -> offset."""
    exp = db.table("experiments")
    ids = [r["id"] for r in exp.select(["id"])]
    if not ids:
        raise DBError("таблица experiments пуста: выполните seed и gen")
    random.seed(1)
    keys = [random.choice(ids) for _ in range(k)]

    def run_all():
        start = time.perf_counter()
        for key in keys:
            exp.select(where=[("id", "=", key)])
        return time.perf_counter() - start

    exp.use_index = False
    seq = run_all()
    exp.use_index = True
    start = time.perf_counter()
    exp.build_index()
    build = time.perf_counter() - start
    idx = run_all()
    print(f"записей в experiments: {len(ids)}, поисков по id: {k}")
    print(f"без индекса (Seq Scan):   {seq * 1000:9.2f} мс всего, {seq / k * 1000:8.3f} мс на поиск")
    print(f"с индексом (Index Scan):  {idx * 1000:9.2f} мс всего, {idx / k * 1000:8.3f} мс на поиск"
          f"  (+ {build * 1000:.2f} мс на построение индекса)")
    print(f"ускорение поиска: x{seq / max(idx, 1e-9):.1f}")
    if len(ids) < 1000:
        print("подсказка: для наглядности выполните gen 5000 и повторите bench")


def kv(args):
    out = {}
    for a in args:
        if "=" not in a:
            raise DBError(f"ожидалось col=val, получено {a!r}")
        k, v = a.split("=", 1)
        out[k] = v
    return out


def parse_where(t):
    """where a = 1 and b > 2 or c = 3  ->  [[(a,=,1),(b,>,2)], [(c,=,3)]]  (группы AND через OR)"""
    if not t:
        return []
    if t[0].lower() != "where":
        raise DBError("ожидалось WHERE")
    t, groups, group, i = t[1:], [], [], 0
    if not t:
        raise DBError("после WHERE нужно условие")
    while i < len(t):
        cond = t[i:i + 3]
        if len(cond) < 3:
            raise DBError("условие: col op value [and|or col op value ...]")
        group.append(tuple(cond))
        i += 3
        if i < len(t):
            sep = t[i].lower()
            if sep == "or":
                groups.append(group)
                group = []
            elif sep != "and":
                raise DBError("между условиями должно стоять and или or")
            i += 1
            if i >= len(t):
                raise DBError("после and/or нужно условие")
    groups.append(group)
    return groups


def show(rows):
    if not rows:
        return print("(пусто)")
    print(" | ".join(rows[0]))
    for r in rows:
        print(" | ".join(str(v) for v in r.values()))
    print(f"({len(rows)} строк)")


def run(db, line):
    t = shlex.split(line)
    if not t:
        return
    cmd = t[0].lower()
    if cmd == "help":
        return print(HELP)
    if cmd == "tables":
        return print(", ".join(db.tables) or "(нет таблиц)")
    if cmd == "seed":
        return seed(db) or print("ok")
    if cmd == "gen":
        return gen(db, int(t[1]))
    if cmd == "bench":
        return bench(db, int(t[1]) if len(t) > 1 else 300)
    if cmd == "backup":
        return db.backup(t[1]) or print("резервная копия:", t[1])
    if cmd == "restore":
        return db.restore(t[1]) or print("восстановлено из:", t[1])
    if cmd == "create":
        return db.create_table(t[1], [a.replace(":", " ") for a in t[2:]]) or print("ok")
    tbl = db.table(t[1])
    w = next((i for i, x in enumerate(t) if x.lower() == "where"), len(t))
    where = parse_where(t[w:])
    if cmd == "insert":
        print("добавлено:", tbl.insert(**kv(t[2:])))
    elif cmd == "select":
        joins, i = [], 2
        while i < w and t[i].lower() == "join":            # join <таблица> on <а> = <б>
            if i + 5 >= w or t[i + 2].lower() != "on" or t[i + 4] != "=":
                raise DBError("join: join <таблица> on <т1.поле> = <т2.поле>")
            joins.append((t[i + 1], t[i + 3], t[i + 5]))
            i += 6
        if w - i > 1:
            raise DBError("столбцы перечисляются через запятую без пробелов: id,name")
        cols = t[i].split(",") if w - i == 1 and t[i] != "*" else None
        if joins:
            show(db.join(t[1], joins, cols, where))
        else:
            show(tbl.select(cols, where))
            print("план:", tbl.plan)
    elif cmd == "update":
        if t[2].lower() != "set":
            raise DBError("ожидалось SET")
        print("изменено:", tbl.update(kv(t[3:w]), where))
    elif cmd == "delete":
        print("удалено:", tbl.delete(where))
    else:
        raise DBError(f"неизвестная команда: {cmd} (help - список команд)")


def main():
    db = DB(sys.argv[1] if len(sys.argv) > 1 else "data")
    print("MiniDB. Введите help для списка команд.")
    while True:
        try:
            line = input("minidb> ")
        except EOFError:
            break
        if line.strip().lower() in ("exit", "quit"):
            break
        try:
            run(db, line)
        except IndexError:
            print("Ошибка: неполная команда (help)")
        except (DBError, ValueError) as e:
            print("Ошибка:", e)


if __name__ == "__main__":
    main()
