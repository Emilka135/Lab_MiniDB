"""MiniDB: файловая мини-СУБД.
Таблица = <name>.meta (схема, текст) + <name>.dat (бинарные записи фиксированной длины).
Запись: 1 байт "жива/удалена" + поля (INT=8 байт, FLOAT=8 байт, STRING=64 байта).
Индекс PRIMARY KEY -> смещение записи в .dat строится в памяти при первом обращении."""
import operator
import os
import struct
import zipfile


class DBError(Exception):
    pass


TYPES = {"INT": ("q", int), "FLOAT": ("d", float), "STRING": ("64s", str)}
OPS = {"=": operator.eq, "!=": operator.ne, ">": operator.gt,
       "<": operator.lt, ">=": operator.ge, "<=": operator.le}


def op_of(o):
    if o not in OPS:
        raise DBError(f"неизвестный оператор: {o}")
    return OPS[o]


def groups_of(where):
    """where: [(поле, оператор, значение), ...] - условия через AND;
    либо [[...], [...]] - группы AND, соединённые через OR."""
    return where if where and isinstance(where[0], list) else [list(where)]


class Table:
    def __init__(self, db, name):
        self.db, self.name = db, name
        base = os.path.join(db.path, name)
        self.meta_path, self.dat_path = base + ".meta", base + ".dat"
        self.types, self.pk, self.fks = {}, None, {}   # fks: колонка -> (таблица, колонка)
        with open(self.meta_path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                col, typ, *flags = line.split()
                self.types[col] = typ
                for fl in flags:
                    if fl == "pk":
                        self.pk = col
                    else:
                        self.fks[col] = tuple(fl[3:].split("."))
        self.fmt = "<?" + "".join(TYPES[t][0] for t in self.types.values())
        self.size = struct.calcsize(self.fmt)
        self.index = None        # индекс: значение PK -> смещение записи (строится лениво)
        self.use_index = True    # можно выключить, чтобы сравнить Seq Scan и Index Scan
        self.plan = "-"          # как выполнялся последний поиск (аналог EXPLAIN)

    # ---------- низкий уровень ----------
    def _decode(self, chunk):
        alive, *vals = struct.unpack(self.fmt, chunk)
        return alive, {c: v.rstrip(b"\0").decode() if t == "STRING" else v
                       for (c, t), v in zip(self.types.items(), vals)}

    def _scan(self):
        """Читает файл запись за записью, отдаёт (offset, строка-словарь) живых записей."""
        with open(self.dat_path, "rb") as f:
            off = 0
            while chunk := f.read(self.size):
                alive, row = self._decode(chunk)
                if alive:
                    yield off, row
                off += self.size

    def _pack(self, row):
        vals = [row[c].encode() if t == "STRING" else row[c] for c, t in self.types.items()]
        return struct.pack(self.fmt, True, *vals)

    def build_index(self):
        """Один проход по файлу: запоминаем, где лежит запись с каждым PK."""
        self.index = {r[self.pk]: o for o, r in self._scan()}
        return self.index

    def _idx(self):
        return self.index if self.index is not None else self.build_index()

    def _col(self, c):
        if c not in self.types:
            raise DBError(f"неизвестное поле: {c}")
        return self.types[c]

    def _cast(self, c, v):
        t = self._col(c)
        try:
            v = TYPES[t][1](v)
        except (ValueError, TypeError):
            raise DBError(f"поле {c}: ожидается {t}, получено {v!r}")
        if t == "STRING" and len(v.encode()) > 64:
            raise DBError(f"поле {c}: строка длиннее 64 байт")
        if t == "INT" and not -2**63 <= v < 2**63:
            raise DBError(f"поле {c}: число не помещается в 8 байт")
        return v

    def _cast_row(self, row):
        """Приводит типы и проверяет внешние ключи."""
        row = {c: self._cast(c, v) for c, v in row.items()}
        for c, (t, tc) in self.fks.items():
            if not self.db.table(t).select(where=[(tc, "=", row[c])]):
                raise DBError(f"нарушение FK: в {t} нет записи с {tc}={row[c]}")
        return row

    def _find(self, where):
        groups = [[(c, op_of(o), self._cast(c, v)) for c, o, v in g] for g in groups_of(where)]
        match = lambda r, g: all(f(r[c], v) for c, f, v in g)
        if self.use_index and len(groups) == 1:
            for c, f, v in groups[0]:
                if c == self.pk and f is operator.eq:        # Index Scan: прыжок к записи
                    self.plan = f"Index Scan по {self.pk}"
                    off = self._idx().get(v)
                    if off is None:
                        return []
                    with open(self.dat_path, "rb") as fh:
                        fh.seek(off)
                        alive, r = self._decode(fh.read(self.size))
                    return [(off, r)] if alive and match(r, groups[0]) else []
        self.plan = "Seq Scan"                                # полный просмотр файла
        return [(o, r) for o, r in self._scan() if any(match(r, g) for g in groups)]

    # ---------- CRUD ----------
    def insert(self, **row):
        extra = set(row) - set(self.types)
        if extra:
            raise DBError(f"неизвестные поля: {', '.join(extra)}")
        if row.get(self.pk) in (None, ""):                      # авто-ID
            if self.types[self.pk] != "INT":
                raise DBError("PRIMARY KEY нужно указать явно")
            row[self.pk] = max(self._idx(), default=0) + 1
        missing = set(self.types) - set(row)
        if missing:
            raise DBError(f"не указаны поля: {', '.join(missing)}")
        row = self._cast_row(row)
        if self._find([(self.pk, "=", row[self.pk])]):
            raise DBError(f"дубликат PRIMARY KEY: {self.pk}={row[self.pk]}")
        off = os.path.getsize(self.dat_path)
        with open(self.dat_path, "ab") as f:
            f.write(self._pack(row))
        if self.index is not None:
            self.index[row[self.pk]] = off
        return row

    def select(self, cols=None, where=()):
        for c in cols or ():
            self._col(c)
        return [{c: r[c] for c in cols or self.types} for _, r in self._find(where)]

    def update(self, values, where=()):
        if self.pk in values:
            raise DBError("нельзя менять PRIMARY KEY")
        new = [(o, self._cast_row({**r, **values})) for o, r in self._find(where)]
        with open(self.dat_path, "r+b") as f:
            for o, r in new:
                f.seek(o)
                f.write(self._pack(r))
        return len(new)

    def delete(self, where=()):
        rows = self._find(where)
        for _, r in rows:                                       # на запись не должны ссылаться
            for other in self.db.tables.values():
                for c, (t, tc) in other.fks.items():
                    if t == self.name and other.select(where=[(c, "=", r[tc])]):
                        raise DBError(f"нельзя удалить: на запись ссылается {other.name}.{c}")
        with open(self.dat_path, "r+b") as f:
            for o, _ in rows:
                f.seek(o)
                f.write(b"\0")                                  # флаг "удалена"
        if self.index is not None:
            for _, r in rows:
                self.index.pop(r[self.pk], None)
        return len(rows)


class DB:
    def __init__(self, path="data"):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self.load()

    def load(self):
        self.tables = {f[:-5]: Table(self, f[:-5])
                       for f in sorted(os.listdir(self.path)) if f.endswith(".meta")}

    def table(self, name):
        if name not in self.tables:
            raise DBError(f"нет таблицы: {name}")
        return self.tables[name]

    def create_table(self, name, cols):
        """cols: ["id INT pk", "dataset_id INT fk=datasets.id", "name STRING"]"""
        if name in self.tables:
            raise DBError(f"таблица {name} уже существует")
        lines = []
        for c in cols:
            p = c.split()
            if len(p) < 2 or p[1].upper() not in TYPES:
                raise DBError(f"неверное описание поля: {c}")
            p[1] = p[1].upper()
            for fl in p[2:]:
                if fl != "pk":
                    ref = fl[3:].split(".") if fl.startswith("fk=") else []
                    if len(ref) != 2 or ref[0] not in self.tables or ref[1] not in self.tables[ref[0]].types:
                        raise DBError(f"неверный флаг: {fl}")
            lines.append(" ".join(p))
        if sum(l.split()[2:].count("pk") for l in lines) != 1:
            raise DBError("нужен ровно один PRIMARY KEY")
        base = os.path.join(self.path, name)
        with open(base + ".meta", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        open(base + ".dat", "wb").close()
        self.tables[name] = Table(self, name)

    # ---------- INNER JOIN ----------
    def join(self, base, joins, cols=None, where=()):
        """joins = [(таблица, "т1.поле", "т2.поле"), ...]. Ключи результата: 'таблица.поле'."""
        names = [base]
        rows = [{f"{base}.{k}": v for k, v in r.items()} for r in self.table(base).select()]

        def ref(x):                                   # "таблица.поле" -> (таблица, поле)
            tb, _, col = x.partition(".")
            if tb not in names:
                raise DBError(f"таблица {tb!r} не участвует в запросе: {x}")
            self.tables[tb]._col(col)
            return self.tables[tb], col

        for tname, a, b in joins:
            t = self.table(tname)
            if tname in names:
                raise DBError(f"таблица {tname} уже подключена")
            if a.partition(".")[0] != tname:
                a, b = b, a                            # a - поле новой таблицы, b - уже подключённой
            if a.partition(".")[0] != tname or b.partition(".")[0] not in names:
                raise DBError(f"JOIN {tname}: условие должно связывать {tname} с подключённой таблицей")
            names.append(tname)
            _, col = ref(a)
            ref(b)
            rows = [{**r, **{f"{tname}.{k}": v for k, v in m.items()}}
                    for r in rows for m in t.select(where=[(col, "=", r[b])])]   # по PK - через индекс

        def cast(x, v):
            t, col = ref(x)
            return t._cast(col, v)

        groups = [[(c, op_of(o), cast(c, v)) for c, o, v in g] for g in groups_of(where)]
        rows = [r for r in rows if any(all(f(r[c], v) for c, f, v in g) for g in groups)]
        for c in cols or ():
            ref(c)
        return [{c: r[c] for c in cols or r} for r in rows]

    # ---------- backup / restore ----------
    def backup(self, archive):
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(os.listdir(self.path)):
                if f.endswith((".meta", ".dat")):
                    z.write(os.path.join(self.path, f), f)

    def restore(self, archive):
        try:
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                if not names or any(os.path.basename(n) != n or not n.endswith((".meta", ".dat")) for n in names):
                    raise DBError("архив не похож на резервную копию MiniDB")
                for f in os.listdir(self.path):         # старое состояние удаляем только после проверки архива
                    if f.endswith((".meta", ".dat")):
                        os.remove(os.path.join(self.path, f))
                z.extractall(self.path)
        except (OSError, zipfile.BadZipFile) as e:
            raise DBError(f"не удалось прочитать архив: {e}")
        self.load()
