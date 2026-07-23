#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build AE-ready tables from the TS26 content plan."""

import csv
import hashlib
import io
import json
import re
import urllib.parse


TIME_HEADER = "ВРЕМЯ"
COMP_NAME_HEADER = "ИМЯ_КОМПОЗИЦИИ"

TOPIC_FIELDS = ["topic_id", "ТЕМА", "ОПИСАНИЕ", "ИСХОДНАЯ_ЯЧЕЙКА"]
VENUE_FIELDS = ["venue_id", "source_column", "ПЛОЩАДКА", "ЦВЕТ"]
SESSION_MODEL_FIELDS = [
    "session_id", "topic_id", "ДЕНЬ", "ДАТА", "ВРЕМЯ", "НАЧАЛО", "КОНЕЦ",
    "venue_id", "ПЛОЩАДКА", "ФОРМАТ", "ТИП_ГРАФИКИ", "ИСХОДНАЯ_ЯЧЕЙКА",
]
SESSION_PEOPLE_FIELDS = [
    "session_id", "person_id", "ФИО спикера", "РОЛЬ", "Должность", "badge_needed",
    "card_needed", "ИСХОДНАЯ_ЯЧЕЙКА",
]
PEOPLE_FIELDS = ["person_id", "ФИО спикера", "normalized_name", "Должность", "Фото на плашку", "ИСХОДНЫЕ_ЯЧЕЙКИ"]
BADGE_FIELDS = ["session_id", "person_id", "ДЕНЬ", "ДАТА", "ВРЕМЯ", "НАЧАЛО", "ПЛОЩАДКА", "ФИО спикера", "Должность", "Фото на плашку"]
CARD_FIELDS = ["person_id", "ФИО спикера", "Должность", "Фото на плашку", "card_status", "card_warning"]
LEGACY_SESSION_FIELDS = ["ДЕНЬ", "ДАТА", "ВРЕМЯ", "ПЛОЩАДКА", "ТЕМА", "ОПИСАНИЕ", "ТИП", COMP_NAME_HEADER, "ИСХОДНАЯ_ЯЧЕЙКА"]
WARNING_FIELDS = ["level", "source_cell", "message", "raw_text", "confidence"]
SOURCE_CELL_FIELDS = ["source_cell", "ДЕНЬ", "ДАТА", "ВРЕМЯ", "ПЛОЩАДКА", "raw_text", "parser_topic", "parser_people_count", "llm_applied", "llm_confidence"]
REPORT_FIELDS = ["key", "value"]

DEFAULT_VENUES = [
    {"venue_id": "amphitheater", "source_column": "B", "column_index": 1, "name": "Амфитеатр", "color": "red"},
    {"venue_id": "ural_1", "source_column": "C", "column_index": 2, "name": "Урал 1", "color": "blue"},
    {"venue_id": "ural_2", "source_column": "D", "column_index": 3, "name": "Урал 2", "color": "red"},
]

SHEET_TABS = [
    ("content_plan_sessions", LEGACY_SESSION_FIELDS, "legacy_sessions"),
    ("content_plan_plates", BADGE_FIELDS, "badges"),
    ("content_plan_cards", CARD_FIELDS, "cards"),
    ("content_plan_all_people", PEOPLE_FIELDS, "people"),
    ("content_plan_topics_model", TOPIC_FIELDS, "topics"),
    ("content_plan_sessions_model", SESSION_MODEL_FIELDS, "sessions"),
    ("content_plan_session_people", SESSION_PEOPLE_FIELDS, "session_people"),
    ("import_report", REPORT_FIELDS, "report_rows"),
    ("warnings", WARNING_FIELDS, "warnings"),
    ("source_cells", SOURCE_CELL_FIELDS, "source_cells"),
]

ROLE_RE = re.compile(r"(?is)(Эксперты?|Эксперт|Гости|Спикеры?|Спикер|Модератор|Ведущий)\s*:\s*")
STOP_RE = re.compile(r"(?is)(?:^|\s)(?:▶\s*)?(?:Статус|СЦЕНАРИЙ(?:\s+ДЛЯ\s+РПГ)?|ЗАЛ|СЕТАП|РАЙДЕР|КОНТЕНТ|ВОЛОНТЕРЫ|Техзапрос|Техзадание|Место)\s*:")
SERVICE_RE = re.compile(r"(?i)^(перерыв|обед|ужин|завтрак|зарядка|отъезд|подъ[её]м|рефлексия|креатон(?:\s*-.*)?|\d+)$")
NAME_RE = re.compile(r"((?:[А-ЯЁA-Z]\.\s*){1,3}[А-ЯЁA-Z]?\.\s*[А-ЯЁA-Z][а-яё-]+|[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?)")


class AEContentPlanError(Exception):
    pass


def inline_text(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u00a0\t]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(value):
    return re.sub(r"[^0-9a-zа-яё]+", "", inline_text(value).lower().replace("ё", "е"))


def stable_id(prefix, value):
    key = normalize_key(value)
    return "{}_{}".format(prefix, key[:80] or "unknown")


def guess_delimiter(text):
    first = str(text or "").splitlines()[0] if str(text or "").splitlines() else ""
    counts = {"\t": first.count("\t"), ",": first.count(","), ";": first.count(";")}
    return max(counts, key=counts.get) if max(counts.values()) else "\t"


def parse_table_rows(text):
    text = str(text or "").replace("\r\n", "\n")
    delimiter = guess_delimiter(text)
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))


def clean_venue_header(value):
    text = inline_text(value)
    text = re.sub(r"\(\s*(?:до\s*)?\d+\s*(?:мест[а]?|чел(?:овек)?\.?)\s*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:до\s*)?\d+\s*(?:мест[а]?|чел(?:овек)?\.?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -—")
    return text[:1].upper() + text[1:] if text.isupper() and len(text) > 1 else text


def clean_topic(value):
    text = inline_text(value)
    text = re.sub(r"^(?:тема|название|сессия)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[▶•\s]+", "", text)
    text = re.sub(r"\s+(?:▶\s*)?(?:статус|эксперты?|спикеры?|гости?|модератор|ведущий|зал|сетап|райдер|контент|техзадание)\s*:.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[-–—]\s*ПРЕЗЕНТАЦИ[ЯИ]\b.*$", "", text, flags=re.IGNORECASE)
    return text.strip(" \"'«»„“”.,;:-–—")


def clean_position(value):
    text = inline_text(value)
    text = re.sub(r"\((?:подтвержден[аы]?|уточняется)\)", " ", text, flags=re.IGNORECASE)
    text = re.split(r"(?i)\s+(?:СЦЕНАРИЙ(?:\s+ДЛЯ\s+РПГ)?|ЗАЛ|СЕТАП|РАЙДЕР|КОНТЕНТ|ВОЛОНТЕРЫ|Техзапрос|Техзадание)\s*:?", text, maxsplit=1)[0]
    return inline_text(text).strip(" .,-–—;")


def session_comp_name(venue_name, topic_title):
    topic = clean_topic(topic_title)
    venue = clean_venue_header(venue_name)
    return "{}/{}".format(venue, topic) if venue and topic else topic


def detect_layout(rows):
    for row_index, row in enumerate(rows[:30]):
        for col_index, value in enumerate(row):
            if inline_text(value).upper() == TIME_HEADER:
                return {"header_row": row_index, "time_column": col_index}
    raise AEContentPlanError("Не найдена строка заголовка с колонкой '{}'.".format(TIME_HEADER))


def venues_from_rows(rows, layout):
    header_row = rows[layout["header_row"]] if layout["header_row"] < len(rows) else []
    venues = []
    for fallback in DEFAULT_VENUES:
        item = dict(fallback)
        index = item["column_index"]
        item["name"] = clean_venue_header(header_row[index] if index < len(header_row) else "") or fallback["name"]
        venues.append(item)
    return venues


def parse_day(value):
    match = re.search(r"ДЕНЬ\s+(\d+).*?(\d{1,2}\.\d{1,2}|ДД\.ММ)", inline_text(value), re.IGNORECASE)
    if not match:
        return None
    return {"day": "ДЕНЬ {}".format(match.group(1)), "date": match.group(2)}


def is_time(value):
    return re.match(r"^(?:до\s*)?\d{1,2}[:.]\d{2}(?:\s*[–-]\s*\d{1,2}[:.]\d{2})?$", inline_text(value)) is not None


def split_time(value):
    text = inline_text(value)
    match = re.match(r"^(?:до\s*)?(\d{1,2}[:.]\d{2})(?:\s*[–-]\s*(\d{1,2}[:.]\d{2}))?$", text)
    if not match:
        return text, "", ""
    return text, match.group(1).replace(".", ":"), (match.group(2) or "").replace(".", ":")


def strip_file_tokens(text):
    text = re.sub(r"\S+\.(?:docx|doc|pdf|pptx|xlsx)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"СЦЕНАРИЙ\s+ДЛЯ\s+РПГ\s*:\s*\S+", " ", text, flags=re.IGNORECASE)
    return inline_text(text)


def extract_topic_and_description(cell):
    text = inline_text(cell)
    topic_match = re.search(
        r"(?is)(?:^|\s)Тема\s*:\s*(.+?)(?=\s+(?:Эксперты?|Гости|Спикеры?|Эксперт|Модератор|Ведущий|СЦЕНАРИЙ(?:\s+ДЛЯ\s+РПГ)?|ЗАЛ|СЕТАП|РАЙДЕР|КОНТЕНТ)\s*:|$)",
        text,
    )
    if topic_match:
        topic = clean_topic(topic_match.group(1))
        description = strip_file_tokens(text[: topic_match.start()]).strip(" -—")
        return topic, description
    first_role = ROLE_RE.search(text)
    head = text[: first_role.start()] if first_role else text
    head = strip_file_tokens(head).strip(" -—")
    quote = re.search(r"«([^»\n]{8,})»", head) or re.search(r"\"([^\"\n]{8,})\"", head)
    if quote:
        return clean_topic(quote.group(1)), head
    if first_role and len(head) > 10 and not SERVICE_RE.match(head):
        return clean_topic(head), head
    return "", ""


def is_content_cell(cell):
    text = inline_text(cell)
    if not text or SERVICE_RE.match(text):
        return False
    return bool(ROLE_RE.search(text) or re.search(r"(?i)(?:^|\s)Тема\s*:", text) or len(NAME_RE.findall(text)) >= 2)


def validate_person_name(value):
    text = inline_text(value)
    text = re.sub(r"^[▶•\-–—\s]+", "", text).strip(" .,-–—;:")
    if not text or re.search(r"\d|https?://|www\.|@|[=<>/_]", text, flags=re.IGNORECASE):
        return ""
    parts = [part.strip(" .,-–—()[]") for part in re.split(r"[\s,;]+", text) if part.strip(" .,-–—()[]")]
    if len(parts) < 2 or len(parts) > 4:
        return ""
    if not all(re.fullmatch(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’.:-]*", part) for part in parts):
        return ""
    return " ".join(parts).replace(".", ". ")


def split_people_block(block):
    text = STOP_RE.split(inline_text(block), maxsplit=1)[0]
    text = re.sub(r"\((?:подтвержден[аы]?|уточняется)\)", " ", text, flags=re.IGNORECASE)
    starts = [match.start(1) for match in NAME_RE.finditer(text) if validate_person_name(match.group(1))]
    if not starts:
        return []
    starts.append(len(text))
    return [text[starts[i] : starts[i + 1]].strip(" ;.-") for i in range(len(starts) - 1)]


def parse_person(piece):
    text = inline_text(piece)
    if "," in text:
        name, position = text.split(",", 1)
    else:
        match = NAME_RE.match(text)
        if not match:
            return None
        name = match.group(1)
        position = text[match.end(1) :]
    name = validate_person_name(name)
    if not name:
        return None
    return {"name": name, "position": clean_position(position), "normalized_name": normalize_key(name)}


def extract_people(cell):
    text = inline_text(cell)
    people = []
    matches = list(ROLE_RE.finditer(text))
    for index, match in enumerate(matches):
        role = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        for piece in split_people_block(text[start:end]):
            person = parse_person(piece)
            if person:
                person["role"] = role
                people.append(person)
    prefix = text[: matches[0].start()] if matches else text
    if len(NAME_RE.findall(prefix)) >= 2:
        for piece in split_people_block(prefix):
            person = parse_person(piece)
            if person:
                person["role"] = "Спикер"
                people.append(person)
    return people


def detect_format(cell, description):
    text = inline_text(description) or inline_text(cell)
    text = re.sub(r"\S+\.(?:docx|doc|pdf|pptx|xlsx)", " ", text, flags=re.IGNORECASE)
    return inline_text(re.split(r"(?i)(?:^|\s)Тема\s*:", text, maxsplit=1)[0]).strip(" -—")


def graphic_type(cell):
    text = inline_text(cell).lower()
    return "card" if "мастер-класс" in text or "программа по выбору" in text else "badge"


def apply_llm_correction(parsed, correction, confidence_threshold):
    if not isinstance(correction, dict):
        return parsed, False, 0.0, ["LLM вернула не JSON-объект."]
    try:
        confidence = float(correction.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    warnings = [inline_text(item) for item in correction.get("warnings") or [] if inline_text(item)]
    if confidence < confidence_threshold:
        return parsed, False, confidence, warnings + ["LLM confidence ниже порога."]
    result = dict(parsed)
    topic = clean_topic(correction.get("topic", ""))
    if topic and (not result.get("topic") or normalize_key(topic) == normalize_key(result.get("topic"))):
        result["topic"] = topic
    elif topic and result.get("topic"):
        warnings.append("LLM topic отличается от регулярного parse; оставлен регулярный.")
    description = inline_text(correction.get("description", ""))
    if description:
        result["description"] = description
    fmt = inline_text(correction.get("format", ""))
    if fmt:
        result["format"] = fmt
    people = []
    for item in correction.get("people") or []:
        if not isinstance(item, dict):
            continue
        name = validate_person_name(item.get("name", ""))
        if not name:
            continue
        people.append({
            "name": name,
            "position": clean_position(item.get("position", "")),
            "role": inline_text(item.get("role", "")) or "Спикер",
            "normalized_name": normalize_key(name),
        })
    if people:
        result["people"] = people
    return result, True, confidence, warnings


def add_warning(warnings, level, source_cell, message, raw_text="", confidence=""):
    warnings.append({
        "level": level,
        "source_cell": source_cell,
        "message": message,
        "raw_text": inline_text(raw_text)[:1000],
        "confidence": str(confidence),
    })


def merge_person(people_by_key, person, source_cell):
    key = person["normalized_name"]
    if key not in people_by_key:
        people_by_key[key] = {
            "person_id": stable_id("person", key),
            "ФИО спикера": person["name"],
            "normalized_name": key,
            "positions": [],
            "source_cells": [],
            "Фото на плашку": "",
        }
    item = people_by_key[key]
    if len(person["name"]) > len(item["ФИО спикера"]):
        item["ФИО спикера"] = person["name"]
    if person.get("position") and person["position"] not in item["positions"]:
        item["positions"].append(person["position"])
    if source_cell not in item["source_cells"]:
        item["source_cells"].append(source_cell)
    return item


def build_records(rows, corrector=None, confidence_threshold=0.82):
    layout = detect_layout(rows)
    venues = venues_from_rows(rows, layout)
    venue_by_index = {item["column_index"]: item for item in venues}
    time_column = layout["time_column"]
    current_day = {"day": "", "date": ""}
    found_days = []
    found_time_rows = 0
    topics_by_key = {}
    sessions_by_key = {}
    people_by_key = {}
    session_people_by_key = {}
    warnings = []
    source_cells = []
    ignored_content_cells = 0
    llm_applied_count = 0

    for row_number, row in enumerate(rows, start=1):
        parsed_day = next((parse_day(value) for value in row if parse_day(value)), None)
        if parsed_day:
            current_day = parsed_day
            found_days.append("{} {}".format(parsed_day["day"], parsed_day["date"]))
            continue
        if not current_day["day"]:
            continue
        time_value = inline_text(row[time_column] if time_column < len(row) else "")
        if not is_time(time_value):
            continue
        found_time_rows += 1
        time_label, time_start, time_end = split_time(time_value)
        for column_index, cell in enumerate(row):
            if column_index <= time_column or not is_content_cell(cell):
                continue
            source_cell = "row {}, col {}".format(row_number, chr(ord("A") + column_index))
            venue = venue_by_index.get(column_index)
            if not venue:
                ignored_content_cells += 1
                add_warning(warnings, "warning", source_cell, "Ячейка вне строгих площадок B/C/D не записана в AE-ready sessions.", cell)
                continue
            topic, description = extract_topic_and_description(cell)
            people = extract_people(cell)
            parsed = {"topic": topic, "description": description, "format": detect_format(cell, description), "people": people}
            llm_applied = False
            llm_confidence = ""
            if corrector and (not topic or not people or re.search(r"https?://|\S+\.(?:docx|pdf|pptx)", inline_text(cell), re.IGNORECASE)):
                correction = corrector({
                    "source_cell": source_cell,
                    "day": current_day["day"],
                    "date": current_day["date"],
                    "time": time_label,
                    "venue": venue["name"],
                    "raw_text": inline_text(cell),
                    "parser": parsed,
                })
                parsed, llm_applied, llm_confidence, llm_warnings = apply_llm_correction(parsed, correction, confidence_threshold)
                if llm_applied:
                    llm_applied_count += 1
                for item in llm_warnings:
                    add_warning(warnings, "warning", source_cell, item, cell, llm_confidence)
            topic = parsed.get("topic") or ""
            description = parsed.get("description") or ""
            people = parsed.get("people") or []
            source_cells.append({
                "source_cell": source_cell,
                "ДЕНЬ": current_day["day"],
                "ДАТА": current_day["date"],
                "ВРЕМЯ": time_label,
                "ПЛОЩАДКА": venue["name"],
                "raw_text": inline_text(cell),
                "parser_topic": topic,
                "parser_people_count": len(people),
                "llm_applied": "1" if llm_applied else "0",
                "llm_confidence": str(llm_confidence),
            })
            if not topic and not people:
                add_warning(warnings, "warning", source_cell, "Не удалось уверенно извлечь тему или людей.", cell, llm_confidence)
                continue
            topic_key = normalize_key(topic)
            topic_id = stable_id("topic", topic_key)
            if topic and topic_key not in topics_by_key:
                topics_by_key[topic_key] = {"topic_id": topic_id, "ТЕМА": topic, "ОПИСАНИЕ": description, "ИСХОДНАЯ_ЯЧЕЙКА": source_cell}
            session_key = "|".join([current_day["day"], current_day["date"], time_start, time_end, venue["venue_id"], topic_key])
            session_id = stable_id("session", session_key)
            if session_key not in sessions_by_key:
                sessions_by_key[session_key] = {
                    "session_id": session_id,
                    "topic_id": topic_id if topic else "",
                    "ДЕНЬ": current_day["day"],
                    "ДАТА": current_day["date"],
                    "ВРЕМЯ": time_label,
                    "НАЧАЛО": time_start,
                    "КОНЕЦ": time_end,
                    "venue_id": venue["venue_id"],
                    "ПЛОЩАДКА": venue["name"],
                    "ФОРМАТ": parsed.get("format") or detect_format(cell, description),
                    "ТИП_ГРАФИКИ": graphic_type(cell),
                    "ИСХОДНАЯ_ЯЧЕЙКА": source_cell,
                }
            for person in people:
                merged = merge_person(people_by_key, person, source_cell)
                relation_key = "|".join([session_id, merged["person_id"], normalize_key(person.get("role", ""))])
                if relation_key in session_people_by_key:
                    continue
                card_needed = sessions_by_key[session_key]["ТИП_ГРАФИКИ"] == "card"
                session_people_by_key[relation_key] = {
                    "session_id": session_id,
                    "person_id": merged["person_id"],
                    "ФИО спикера": merged["ФИО спикера"],
                    "РОЛЬ": person.get("role") or "Спикер",
                    "Должность": person.get("position", ""),
                    "badge_needed": "1",
                    "card_needed": "1" if card_needed else "0",
                    "ИСХОДНАЯ_ЯЧЕЙКА": source_cell,
                }

    topics = list(topics_by_key.values())
    sessions = list(sessions_by_key.values())
    people = [{
        "person_id": item["person_id"],
        "ФИО спикера": item["ФИО спикера"],
        "normalized_name": item["normalized_name"],
        "Должность": " | ".join(item["positions"]),
        "Фото на плашку": item["Фото на плашку"],
        "ИСХОДНЫЕ_ЯЧЕЙКИ": " | ".join(item["source_cells"]),
    } for item in people_by_key.values()]
    session_people = list(session_people_by_key.values())
    sessions_by_id = {row["session_id"]: row for row in sessions}
    people_by_id = {row["person_id"]: row for row in people}
    badges_by_key = {}
    for relation in session_people:
        session = sessions_by_id.get(relation["session_id"], {})
        person = people_by_id.get(relation["person_id"], {})
        badge_key = "{}|{}".format(relation["session_id"], relation["person_id"])
        if badge_key not in badges_by_key:
            badges_by_key[badge_key] = {
                "session_id": relation["session_id"],
                "person_id": relation["person_id"],
                "ДЕНЬ": session.get("ДЕНЬ", ""),
                "ДАТА": session.get("ДАТА", ""),
                "ВРЕМЯ": session.get("ВРЕМЯ", ""),
                "НАЧАЛО": session.get("НАЧАЛО", ""),
                "ПЛОЩАДКА": session.get("ПЛОЩАДКА", ""),
                "ФИО спикера": person.get("ФИО спикера", relation["ФИО спикера"]),
                "Должность": relation["Должность"] or person.get("Должность", ""),
                "Фото на плашку": person.get("Фото на плашку", ""),
            }
    badges = list(badges_by_key.values())
    card_person_ids = {row["person_id"] for row in session_people if row["card_needed"] == "1"}
    cards = [{
        "person_id": row["person_id"],
        "ФИО спикера": row["ФИО спикера"],
        "Должность": row["Должность"],
        "Фото на плашку": row["Фото на плашку"],
        "card_status": "missing_photo" if not row["Фото на плашку"] else "ready",
        "card_warning": "Нет фото: загрузите фото или создайте черновик" if not row["Фото на плашку"] else "",
    } for row in people if row["person_id"] in card_person_ids]
    if ignored_content_cells:
        add_warning(warnings, "warning", "", "Игнорированы ячейки вне строгих площадок B/C/D: {}".format(ignored_content_cells))
    if any(row["card_status"] == "missing_photo" for row in cards):
        add_warning(warnings, "warning", "", "Есть визитки без фото: {}".format(sum(1 for row in cards if row["card_status"] == "missing_photo")))
    records = {
        "venues": [{"venue_id": item["venue_id"], "source_column": item["source_column"], "ПЛОЩАДКА": item["name"], "ЦВЕТ": item["color"]} for item in venues],
        "topics": topics,
        "sessions": sessions,
        "people": people,
        "session_people": session_people,
        "badges": badges,
        "cards": cards,
        "warnings": warnings,
        "source_cells": source_cells,
    }
    records["legacy_sessions"] = legacy_sessions(records)
    report = {
        "sessions_found": len(sessions),
        "topics_found": len(topics),
        "people_found": len(session_people),
        "unique_people": len(people),
        "badges": len(badges),
        "cards": len(cards),
        "cards_missing_photo": sum(1 for row in cards if row["card_status"] == "missing_photo"),
        "warnings": len(warnings),
        "ignored_non_bcd_cells": ignored_content_cells,
        "time_column": time_column + 1,
        "days": ", ".join(found_days),
        "time_rows": found_time_rows,
        "llm_applied": llm_applied_count,
    }
    records["report"] = report
    records["report_rows"] = [{"key": key, "value": json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)} for key, value in report.items()]
    return records


def legacy_sessions(records):
    topics_by_id = {row["topic_id"]: row for row in records["topics"]}
    rows = []
    for session in records["sessions"]:
        topic = topics_by_id.get(session["topic_id"], {})
        rows.append({
            "ДЕНЬ": session["ДЕНЬ"],
            "ДАТА": session["ДАТА"],
            "ВРЕМЯ": session["ВРЕМЯ"],
            "ПЛОЩАДКА": session["ПЛОЩАДКА"],
            "ТЕМА": topic.get("ТЕМА", ""),
            "ОПИСАНИЕ": topic.get("ОПИСАНИЕ", ""),
            "ТИП": session["ФОРМАТ"],
            COMP_NAME_HEADER: session_comp_name(session["ПЛОЩАДКА"], topic.get("ТЕМА", "")),
            "ИСХОДНАЯ_ЯЧЕЙКА": session["ИСХОДНАЯ_ЯЧЕЙКА"],
        })
    return rows


def records_hash(records):
    payload = {key: records.get(key, []) for _tab, _fields, key in SHEET_TABS if key not in {"report_rows", "warnings", "source_cells"}}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def google_sheet_url(spreadsheet_id):
    return "https://docs.google.com/spreadsheets/d/{}/edit".format(spreadsheet_id)


def source_export_url(url):
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if "docs.google.com" not in parsed.netloc or "/spreadsheets/d/" not in parsed.path:
        return str(url or "").strip()
    match = re.search(r"/spreadsheets/d/([^/]+)", parsed.path)
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    gid = query.get("gid", fragment.get("gid", ["0"]))[0]
    return "https://docs.google.com/spreadsheets/d/{}/export?format=tsv&gid={}".format(match.group(1), gid)
