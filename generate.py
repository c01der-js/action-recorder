#!/usr/bin/env python3
"""
Превращает запись сессии в два документа:
  instructions.md — сценарии для ИИ-агента (по одному на каждую повторяемую задачу)
  summary.md      — резюме о работе сотрудника

Короткая запись обрабатывается одним запросом. Длинная (рабочий день) режется
на фрагменты по ~20 минут: по каждому делаются заметки, затем из заметок —
итоговые сценарии и резюме. Заметки сохраняются в <сессия>/notes/.

  python generate.py sessions/2026-10-22_09-00-00
  python generate.py sessions/... --dry-run   # журнал и оценка объёма без вызова API

Нужен ANTHROPIC_API_KEY в окружении.
"""
import argparse
import base64
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_MODEL = os.environ.get("RECORDER_MODEL", "claude-sonnet-5-5")
IDLE_CAP = 60          # сек: паузы дольше считаем простоем
CHUNK_MINUTES = 20     # длина фрагмента для длинных записей
CHUNK_MAX_EVENTS = 700
SINGLE_PASS_MINUTES = 30
ONEC_EXE = {"1cv8c.exe", "1cv8.exe", "1cv8s.exe"}

CONTEXT = """Ты анализируешь запись работы пользователя за компьютером (Windows или Mac). Часто это сотрудник бухгалтерии или операционист, работающий в 1С:Предприятие.
Журнал: [время] приложение — «заголовок окна» | действие. Пометка «1С» — окно 1С. Красный круг на скриншоте — место клика; скриншот снят в момент нажатия, до реакции интерфейса. «элемент: …» — название элемента под курсором по данным Windows UI Automation: может отсутствовать или быть неточным, тогда опирайся на скриншот. Двойной клик записан как clicks=1 и clicks=2. Координаты — пиксели экрана, только запасная подсказка. «буфер обмена» — что скопировано/вставлено. Если приложение — «Удалённый рабочий стол», запись сделана на компьютере оператора, а работа идёт на сервере: что именно происходит, определяй по скриншотам.
Особенности 1С: панель разделов, списки документов и справочников, формы документов с шапкой и табличной частью; кнопки «Записать», «Провести», «Провести и закрыть»; F4 или «…» — выбор из справочника, F8 — создать, F9 — скопировать, Ins — добавить строку, Ctrl+Enter — провести и закрыть. Называй разделы, документы, поля и кнопки так, как они подписаны на экране."""

RULES = """Правила: описывай намерение, а не сырые события. Склей серии кликов и ввода в осмысленные шаги, выкинь случайные клики, исправленные опечатки, блуждание и простои. Если смысл шага неясен — пометь ⚠️ и предложи трактовку. Пароли, коды, номера карт и счетов не переписывай — подставляй {{переменная}}. Перед необратимыми действиями (проведение, отправка, оплата, удаление) добавь шаг «запросить подтверждение у пользователя»."""

FINAL_FORMAT = """Сделай два документа в Markdown.

<instructions> — сценарии для ИИ-агента, который управляет компьютером (computer use) и сам повторит эту работу. Если в записи несколько разных задач — отдельный сценарий на каждую повторяемую или значимую задачу; одноразовые мелочи пропусти. Каждый сценарий:
### Сценарий N: название
**Цель** — одна-две фразы.
**Когда запускать и исходные условия** — какие программы, база 1С, права, что должно быть готово.
**Переменные** — данные, которые меняются от раза к разу (контрагент, сумма, дата, номер), в виде {{имя}} с примером из записи.
**Шаги** — нумерованный список: действие; где (раздел → список → форма → поле/кнопка по подписи); ожидаемый результат для проверки.
**Проверка результата** — как понять, что задача выполнена.
**Риски** — что может пойти не так и как реагировать.
**Как надёжнее** — если задачу правильнее делать не кликами, а через загрузку файла, обработку 1С, обмен или API (OData, HTTP-сервис), скажи это.
""" + RULES + """

<summary> — резюме о работе сотрудника, кратко и по делу:
- Что сделано — по задачам; для 1С — какие документы, справочники, отчёты и примерно сколько.
- Время — по приложениям и по задачам (цифры только из СТАТИСТИКИ и отметок времени).
- Рутина — что повторялось и сколько раз; что автоматизировать, чем, и примерная экономия времени.
- Ошибки, переделки, лишние действия, простои.

Ответ — только два блока: <instructions>…</instructions> и <summary>…</summary>."""

CHUNK_FORMAT = """Это фрагмент {i} из {n} длинной записи. Сценарии и резюме сейчас не пиши — только подробные заметки для следующего этапа:
<notes>
1. Задачи во фрагменте: интервал [ч:мм:сс–ч:мм:сс], что сделано, результат (например «введено поступление от {{контрагент}} на сумму …, проведено»).
2. Для каждой задачи — точная последовательность шагов: раздел, список, форма, поля, кнопки по подписям на экране, вводимые значения (пароли и номера счетов не переписывать).
3. Повторы: какие задачи и действия повторялись и сколько раз.
4. Ошибки, переделки, простои, неясные места.
</notes>"""


def fmt_t(t):
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def app_label(e):
    app = e.get("app") or "?"
    if app.lower() == "mstsc.exe":
        return "Удалённый рабочий стол"
    if e.get("onec"):
        return "1С" if app.lower() in ONEC_EXE else f"1С (веб, {app})"
    return app[:-4] if app.lower().endswith(".exe") else app


def load(session: Path):
    def jl(name):
        p = session / name
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    events = sorted(jl("events.jsonl"), key=lambda e: e["t"])  # sorted() устойчива
    elements = {el["ref"]: el for el in jl("elements.jsonl") if "ref" in el}
    meta_p = session / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    return events, elements, meta


def compute_stats(events):
    per_app, idle, paused = Counter(), 0.0, False
    for a, b in zip(events, events[1:]):
        if a["type"] == "pause":
            paused = True
        elif a["type"] == "resume":
            paused = False
        if paused:
            continue
        gap = b["t"] - a["t"]
        if gap <= 0:
            continue
        if gap > IDLE_CAP:
            idle += gap - IDLE_CAP
            gap = IDLE_CAP
        per_app[app_label(a)] += gap
    total = events[-1]["t"] - events[0]["t"] if events else 0
    return {
        "duration": total,
        "active": sum(per_app.values()),
        "idle": idle,
        "per_app": per_app.most_common(),
        "counts": Counter(e["type"] for e in events),
    }


def stats_text(st, meta=None):
    lines = []
    if meta and meta.get("started_at"):
        lines.append(f"Начало записи: {meta['started_at']}" +
                     (f", пользователь: {meta['user']}" if meta.get("user") else ""))
    lines.append(f"Длительность: {fmt_t(st['duration'])}, активно: {fmt_t(st['active'])}, "
                 f"простой: {fmt_t(st['idle'])}")
    lines.append("Время по приложениям:")
    lines += [f"  {app}: {fmt_t(sec)}" for app, sec in st["per_app"]]
    c = st["counts"]
    lines.append(f"Кликов: {c['click']}, перетаскиваний: {c['drag']}, вводов текста: {c['type']}, "
                 f"горячих клавиш: {c['hotkey']}, операций с буфером: {c['clipboard']}, "
                 f"переключений приложений: {c['app_switch']}")
    return "\n".join(lines)


def describe(e, elements):
    t = e["type"]
    el = elements.get(e.get("ref"))
    el_txt = ""
    if el:
        parts = []
        if el.get("name"):
            parts.append(f"«{el['name']}» ({el.get('type')})")
        if el.get("parents"):
            parts.append("в " + " / ".join(f"«{p}»" for p in el["parents"][:2]))
        if parts:
            el_txt = " — элемент: " + " ".join(parts)
    if t == "click":
        n = e.get("clicks", 1)
        kind = {1: "клик", 2: "двойной клик", 3: "тройной клик"}.get(n, f"{n}-кратный клик")
        btn = "" if e.get("button") == "left" else f" ({e.get('button')})"
        return f"{kind}{btn} в ({e['x']},{e['y']}){el_txt}"
    if t == "drag":
        return f"перетаскивание ({e['x']},{e['y']}) → ({e['to_x']},{e['to_y']}){el_txt}"
    if t == "scroll":
        return f"прокрутка dx={e['dx']} dy={e['dy']} в ({e['x']},{e['y']})"
    if t == "type":
        return f"ввод текста: «{e['text']}»"
    if t == "hotkey":
        return f"горячая клавиша {e['keys']}"
    if t == "key":
        n = e.get("times", 1)
        return f"клавиша {e['key']}" + (f" ×{n}" if n > 1 else "")
    if t == "clipboard":
        op = {"copy": "скопировано", "cut": "вырезано", "paste": "вставлено"}.get(e.get("op"), e.get("op"))
        return f"буфер обмена, {op}: «{e.get('text')}»"
    if t == "app_switch":
        return f"переключение → {app_label(e)}"
    if t == "pause":
        return "пауза записи"
    if t == "resume":
        return "запись продолжена"
    return t


def log_line(i, e, elements, with_shot):
    where = app_label(e) + (f" — «{e['title']}»" if e.get("title") else "")
    line = f"#{i} [{fmt_t(e['t'])}] {where} | {describe(e, elements)}"
    return line + (f" [скриншот #{i}]" if with_shot else "")


def split_chunks(events):
    """Список фрагментов: каждый — список (индекс, событие)."""
    items = [(i, e) for i, e in enumerate(events) if e["type"] not in ("start", "stop")]
    if not items:
        return []
    if items[-1][1]["t"] - items[0][1]["t"] <= SINGLE_PASS_MINUTES * 60 and len(items) <= CHUNK_MAX_EVENTS:
        return [items]
    chunks, cur = [], []
    for it in items:
        if cur and (it[1]["t"] - cur[0][1]["t"] > CHUNK_MINUTES * 60 or len(cur) >= CHUNK_MAX_EVENTS):
            chunks.append(cur)
            cur = []
        cur.append(it)
    if cur:
        chunks.append(cur)
    return chunks


def build_blocks(session, items, elements, header, tail, max_images):
    shot_idx = [i for i, e in items if e.get("shot") and (session / e["shot"]).exists()]
    if len(shot_idx) > max_images:
        step = len(shot_idx) / max_images
        keep = {shot_idx[int(k * step)] for k in range(max_images)}
    else:
        keep = set(shot_idx)

    buf = [header, "", "ЖУРНАЛ ДЕЙСТВИЙ:"]
    blocks, lines = [], []
    for i, e in items:
        line = log_line(i, e, elements, i in keep)
        buf.append(line)
        lines.append(line)
        if i in keep:
            blocks.append({"type": "text", "text": "\n".join(buf)})
            buf = []
            data = base64.standard_b64encode((session / e["shot"]).read_bytes()).decode()
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    buf.append("\n" + tail)
    blocks.append({"type": "text", "text": "\n".join(buf)})
    return blocks, lines, len(keep)


def grab(text, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def call(client, model, system, content, max_tokens):
    with client.messages.stream(model=model, max_tokens=max_tokens, system=system,
                                messages=[{"role": "user", "content": content}]) as s:
        msg = s.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text")


def main():
    p = argparse.ArgumentParser(description="Сценарии для агента + резюме по записи сессии")
    p.add_argument("session", type=Path, help="папка сессии из recorder.py")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-images", type=int, default=30,
                   help="скриншотов на запрос (короткая запись) или на фрагмент (длинная), до 100")
    p.add_argument("--workers", type=int, default=4, help="параллельных запросов для фрагментов")
    p.add_argument("--dry-run", action="store_true", help="только показать журнал и оценку объёма")
    a = p.parse_args()

    events, elements, meta = load(a.session)
    if not events:
        raise SystemExit("Журнал пуст")
    st = compute_stats(events)
    stats = stats_text(st, meta)
    chunks = split_chunks(events)
    max_img = min(a.max_images, 100)

    if a.dry_run:
        print(stats, "\n")
        total_img = 0
        for n, ch in enumerate(chunks, 1):
            _, lines, k = build_blocks(a.session, ch, elements, "", "", max_img)
            total_img += k
            print(f"--- Фрагмент {n}/{len(chunks)}: {len(lines)} событий, скриншотов к отправке: {k}")
            print("\n".join(lines))
        print(f"\nИтого фрагментов: {len(chunks)}, скриншотов к отправке: {total_img}")
        return

    import anthropic
    client = anthropic.Anthropic()

    if len(chunks) == 1:
        print(f"Короткая запись: один запрос в {a.model}…")
        blocks, _, k = build_blocks(a.session, chunks[0], elements, "СТАТИСТИКА:\n" + stats,
                                    "Сформируй <instructions> и <summary> по правилам.", max_img)
        print(f"  событий: {len(chunks[0])}, скриншотов: {k}")
        text = call(client, a.model, CONTEXT + "\n\n" + FINAL_FORMAT, blocks, 12000)
    else:
        notes_dir = a.session / "notes"
        notes_dir.mkdir(exist_ok=True)
        print(f"Длинная запись: {len(chunks)} фрагментов, до {max_img} скриншотов в каждом…")

        def do_chunk(arg):
            n, ch = arg
            t0, t1 = fmt_t(ch[0][1]["t"]), fmt_t(ch[-1][1]["t"])
            blocks, _, _ = build_blocks(a.session, ch, elements, f"ФРАГМЕНТ {n}/{len(chunks)}: {t0}–{t1}",
                                        "Составь <notes> по правилам.", max_img)
            sys_prompt = CONTEXT + "\n\n" + CHUNK_FORMAT.format(i=n, n=len(chunks))
            out = call(client, a.model, sys_prompt, blocks, 6000)
            note = grab(out, "notes") or out
            (notes_dir / f"chunk_{n:02d}.md").write_text(f"# Фрагмент {n}: {t0}–{t1}\n\n{note}\n",
                                                          encoding="utf-8")
            print(f"  ✓ фрагмент {n}/{len(chunks)} ({t0}–{t1})")
            return f"## Фрагмент {n}: {t0}–{t1}\n{note}"

        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            notes = list(ex.map(do_chunk, enumerate(chunks, 1)))

        print("Собираю итоговые сценарии и резюме…")
        content = [{"type": "text", "text": "СТАТИСТИКА:\n" + stats + "\n\nЗАМЕТКИ ПО ФРАГМЕНТАМ:\n\n"
                    + "\n\n".join(notes) + "\n\nСформируй <instructions> и <summary> по правилам."}]
        text = call(client, a.model, CONTEXT + "\n\n" + FINAL_FORMAT, content, 16000)

    instructions = grab(text, "instructions") or text
    summary = grab(text, "summary") or ""
    (a.session / "instructions.md").write_text(instructions + "\n", encoding="utf-8")
    (a.session / "summary.md").write_text(
        f"# Резюме сессии\n\n```\n{stats}\n```\n\n{summary}\n", encoding="utf-8")

    print(f"\n✓ {a.session / 'instructions.md'}\n✓ {a.session / 'summary.md'}\n")
    print(summary)


if __name__ == "__main__":
    main()
