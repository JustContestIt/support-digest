#!/usr/bin/env python3
"""Ежедневная сводка для support-отдела: правила по CRM + разбор сообщений чата моделью.

    python svodka.py cases.json --no-llm      только правила, сообщения приводятся как есть
    python svodka.py cases.json               правила + ИИ (нужны LLM_API_KEY и LLM_MODEL)
    python svodka.py cases.json -o out/svodka.md --log out/log.jsonl

Модель задаётся переменными окружения: LLM_API_KEY, LLM_MODEL, LLM_PROVIDER
(openai — любой OpenAI-совместимый сервис, по умолчанию; anthropic), LLM_BASE_URL.
Зависимостей нет, нужен только Python 3.9+.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time
from pathlib import Path

PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")

# Срочность: чем меньше число, тем выше случай в сводке.
OVERDUE, TODAY, TOMORROW, MANUAL, OK = range(5)
LABELS = {OVERDUE: "СРОК ПРОШЁЛ", TODAY: "СРОК СЕГОДНЯ", TOMORROW: "СРОК ЗАВТРА",
          MANUAL: "ПРОЧИТАТЬ ВРУЧНУЮ", OK: "БЕЗ ДЕЙСТВИЙ"}
TYPES = {"hire": "Оформление сотрудника", "dismissal": "Увольнение сотрудника", "delivery": "Поставка карт"}
RELATIONS = {"confirms": "совпадает с CRM", "adds": "в CRM не подтверждено",
             "problem": "расходится с CRM", "unrelated": "к операции не относится"}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
QUOTE_LIMIT = 120  # сообщения короче цитируются дословно, длиннее — в пересказе модели


# ---------- 1. Правила по CRM (без ИИ) ----------

def day_word(d, today):
    word = {-1: "вчера", 0: "сегодня", 1: "завтра"}.get((d - today).days)
    return f"{word} ({d:%d.%m})" if word else f"{d:%d.%m}"


def by_date(deadline, today):
    """Срочность невыполненного условия по дате срока."""
    days = (deadline - today).days
    return OVERDUE if days < 0 else TODAY if days == 0 else TOMORROW if days == 1 else OK


def check(case, now):
    """Что CRM говорит о случае: факты, срочность, стандартная проверка и действие."""
    crm, kind, today = case["crm"], case["type"], now.date()
    if kind == "hire":
        d, done = date.fromisoformat(crm["first_day"]), crm["documents_received"]
        when = f"первый выход — {day_word(d, today)}"
        return dict(
            done=done, bucket=OK if done else by_date(d, today), deadline=datetime.combine(d, time.min),
            crm=f"{when}; документы {'получены' if done else 'ожидаются'}",
            why=f"документов в CRM нет, {when}",
            check="Где сейчас документы и полный ли комплект.",
            action="Получить документы в рабочем канале и внести в CRM до первого выхода.")
    if kind == "dismissal":
        d, done = date.fromisoformat(crm["last_day"]), crm["manager_confirmed"]
        when = f"последний рабочий день — {day_word(d, today)}"
        return dict(
            done=done, bucket=OK if done else by_date(d, today), deadline=datetime.combine(d, time.max),
            crm=f"{when}; подтверждение ответственного руководителя {'есть' if done else 'отсутствует'}",
            why=f"нет подтверждения руководителя, {when}",
            check="Подтверждает ли увольнение сам ответственный руководитель.",
            action="Запросить подтверждение у ответственного руководителя в рабочем канале и отметить "
                   "в CRM; до этого операцию не закрывать.")
    if kind == "delivery":
        done = crm["status"] == "Принято"
        due = datetime.fromisoformat(crm["due"]) if crm.get("due") else None
        facts = [f"доставка до {due:%H:%M} {day_word(due.date(), today)}"] if due else []
        facts.append(f"статус «{crm['status']}»")
        if crm.get("accepted_at"):
            facts.append(f"приёмка подтверждена в {datetime.fromisoformat(crm['accepted_at']):%H:%M}")
        late = bool(due) and not done and now >= due
        due_text = f"{due:%H:%M}" if due and due.date() == today else f"{due:%d.%m %H:%M}" if due else ""
        return dict(
            done=done, bucket=OVERDUE if late else OK, deadline=due or datetime.max,
            crm="; ".join(facts),
            why=f"срок доставки {due_text} прошёл, приёмки в CRM нет" if late else "",
            check="Где курьер и когда он будет на точке.",
            action="Предупредить точку о новом сроке, зафиксировать его в CRM и проконтролировать приёмку.")
    raise ValueError(f"Неизвестный тип операции: {kind}")


# ---------- 2. Разбор сообщений моделью ----------

def post(url, headers, body):
    """POST JSON. Если сервис не принимает temperature (бывает у новых моделей), повторяет без него."""
    while True:
        request = urllib.request.Request(
            url, json.dumps(body).encode("utf-8"),
            {"content-type": "application/json", "user-agent": "svodka/1.0", **headers})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 400 and "temperature" in text and "temperature" in body:
                body = {k: v for k, v in body.items() if k != "temperature"}
                continue
            raise RuntimeError(f"HTTP {e.code}: {text[:300]}") from None


def ask_model(payload):
    """Один запрос к модели: инструкция из prompt.md + данные случая. Возвращает текст ответа."""
    key, model = os.environ["LLM_API_KEY"], os.environ["LLM_MODEL"]
    user = json.dumps(payload, ensure_ascii=False)
    if os.environ.get("LLM_PROVIDER", "openai") == "anthropic":
        base = os.environ.get("LLM_BASE_URL", "https://api.anthropic.com").rstrip("/")
        data = post(f"{base}/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"},
                    {"model": model, "max_tokens": 1024, "temperature": 0, "system": PROMPT,
                     "messages": [{"role": "user", "content": user}]})
        return next(block["text"] for block in data["content"] if block.get("type") == "text")
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    data = post(f"{base}/chat/completions", {"authorization": f"Bearer {key}"},
                {"model": model, "temperature": 0,
                 "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]})
    return data["choices"][0]["message"]["content"]


def parse(raw, messages):
    """Достаёт JSON из ответа и проверяет его по схеме. Автор и текст берутся из исходных данных, не из ответа."""
    data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    items = {int(item["n"]): item for item in data["messages"]}
    result = []
    for n, message in enumerate(messages, 1):
        item = items[n]
        if item["relation"] not in RELATIONS:
            raise ValueError(f"неизвестное relation: {item['relation']!r}")
        result.append(dict(message, claim=str(item["claim"]).strip(), relation=item["relation"],
                           verify=str(item.get("verify") or "").strip()))
    return result


def analyze(case, crm_text, now, llm):
    """Две попытки разбора. При неудаче возвращает None — случай уйдёт на ручное чтение."""
    payload = {"now": f"{now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}",
               "operation": f"{TYPES[case['type']]} {case['id']}", "crm": crm_text,
               "messages": [{"n": n, "from": m["from"], "text": m["text"]}
                            for n, m in enumerate(case["messages"], 1)]}
    error = None
    for _ in range(2):
        try:
            return parse(llm(payload), case["messages"])
        except Exception as e:  # сеть, ключ, формат ответа: исход один
            error = e
    print(f"[{case['id']}] ИИ-разбор не удался: {error}", file=sys.stderr)
    return None


# ---------- 3. Сборка ----------

def process(case, now, llm=None):
    """Один случай: правила, затем разбор сообщений. Модель может поднять случай в сводке, но не опустить."""
    r = {**case, **check(case, now), "chat": []}
    r["label"] = LABELS[r["bucket"]]
    messages = case.get("messages") or []
    if not messages:
        return r
    parsed = analyze(case, r["crm"], now, llm) if llm else None
    if parsed is None:  # ИИ выключен или не справился: сообщения приводим как есть, случай не теряем
        r["chat"] = [dict(m, claim="", relation=None, verify="") for m in messages]
        if r["bucket"] == OK:
            r.update(bucket=MANUAL, label=LABELS[MANUAL], action="Прочитать сообщения и сверить их с CRM.")
    else:
        r["chat"] = parsed
        if r["bucket"] == OK and any(m["relation"] == "problem" for m in parsed):
            r.update(bucket=TODAY, label="РАСХОЖДЕНИЕ С CRM",
                     why="CRM проблемы не показывает, но о ней пишут в переписке",
                     action="Связаться с автором сообщения, проверить факт и исправить запись в CRM.")
    return r


def render(results, now):
    """Сводка в Markdown: сначала случаи, где нужно действовать."""
    results = sorted(results, key=lambda r: (r["bucket"], r["deadline"], r["id"]))
    urgent = [r for r in results if r["bucket"] < MANUAL]
    out = [f"# Сводка для support-отдела — {WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y %H:%M}", "",
           f"Требуют действий: {len(urgent)} из {len(results)}."]
    if urgent:
        first = urgent[0]
        out.append(f"**Начать с {first['id']}** ({TYPES[first['type']].lower()}): {first['why']}.")
    for i, r in enumerate(results, 1):
        need = {MANUAL: "прочитать сообщения — ИИ их не разбирал", OK: "нет"}.get(r["bucket"], f"да — {r['why']}")
        out += ["", f"## {i}. {r['id']} · {TYPES[r['type']]} · {r['label']}", "",
                f"- **Вмешаться:** {need}.",
                f"- **Подтверждено в CRM:** {r['crm']}."]
        for m in r["chat"]:
            text = " ".join(m["text"].split())
            shown = m["claim"] if m["claim"] and len(text) > QUOTE_LIMIT else f"«{text}»"
            out.append(f"- **Из переписки — {RELATIONS.get(m['relation'], 'ИИ не разбирал')}:** {m['from']}: {shown}")
        if not r["chat"]:
            out.append("- **Из переписки:** сообщений нет.")
        verify = " ".join(m["verify"] for m in r["chat"] if m["verify"] and m["relation"] in ("adds", "problem"))
        if verify or r["bucket"] < MANUAL:
            out.append(f"- **Проверить:** {verify or r['check']}")
        if r["bucket"] <= MANUAL:
            out.append(f"- **Дальше:** {r['action']}")
        else:
            out.append("- **Дальше:** " + ("действий не требуется." if r["done"] else "пока без действий — срок ещё не подошёл."))
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сводка для support-отдела: правила по CRM + разбор чата моделью.")
    ap.add_argument("cases", help="JSON с полями now и cases")
    ap.add_argument("--no-llm", action="store_true", help="только правила, без обращения к модели")
    ap.add_argument("-o", "--out", help="куда сохранить сводку (Markdown)")
    ap.add_argument("--log", help="куда дописывать сырые ответы модели (JSONL)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    data = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    now = datetime.fromisoformat(data["now"]) if data.get("now") else datetime.now()

    llm = None
    if not args.no_llm:
        missing = [name for name in ("LLM_API_KEY", "LLM_MODEL") if not os.environ.get(name)]
        if missing:
            sys.exit(f"Не заданы переменные окружения: {', '.join(missing)}. Без модели: добавьте --no-llm.")

        def llm(payload):
            raw = ask_model(payload)
            if args.log:
                Path(args.log).parent.mkdir(parents=True, exist_ok=True)
                with open(args.log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"operation": payload["operation"], "raw": raw}, ensure_ascii=False) + "\n")
            return raw

    text = render([process(case, now, llm) for case in data["cases"]], now)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
