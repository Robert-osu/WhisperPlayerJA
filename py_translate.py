import hashlib, json, re, time, sys
import translatepy

# ---------------- настройки ----------------
path = "nekoko_2.json"
cache_path = path.rsplit(".", 1)[0] + ".translations.json"

MAX_CHARS = 80          # длиннее — режем японский на куски
PAUSE = 1.2             # пауза между запросами (сек), чтобы не ловить лимит
# -------------------------------------------

_tr = translatepy.Translator()

data = json.load(open(path, encoding="utf-8"))
try:
    raw = json.load(open(cache_path, encoding="utf-8"))
    raw_cache = raw.get("translations", {})
    cache = {
        k: (v.get("translation", "") if isinstance(v, dict) else v)
        for k, v in raw_cache.items()
    }
except FileNotFoundError:
    cache = {}

def key(s):
    return hashlib.sha256(
        f"{float(s['start']):.3f}|{float(s['end']):.3f}|{str(s['text']).strip()}".encode()
    ).hexdigest()

def split_jp(text, max_chars=MAX_CHARS):
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r'(?<=[。！？!?、])', text)
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) <= max_chars:
            cur += p
        else:
            if cur:
                chunks.append(cur)
            while len(p) > max_chars:
                chunks.append(p[:max_chars])
                p = p[max_chars:]
            cur = p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]

def translate_one(text, retries=3):
    """Перевод одной строки. При ошибке — пауза и повтор."""
    for attempt in range(retries):
        try:
            r = _tr.translate(text, "ru", "ja")
            t = str(r).strip()
            if t and t.lower() != text.lower():
                return t
        except Exception as e:
            print(f"  [err attempt {attempt+1}] {e}")
            time.sleep(PAUSE * (attempt + 1))
    return ""

# ---------- основной проход ----------
segs = [s for s in data["segments"] if str(s.get("text", "")).strip()]
todo = [s for s in segs if not cache.get(key(s))]
print(f"Всего сегментов: {len(segs)}, к переводу: {len(todo)}")

if not todo:
    print("Нечего переводить — всё в кэше.")
    sys.exit(0)

# 1) разбить все сегменты на куски
all_chunks, seg_ranges = [], []
for s in todo:
    chunks = split_jp(str(s["text"]).strip())
    start = len(all_chunks)
    all_chunks.extend(chunks)
    seg_ranges.append((start, len(all_chunks)))

print(f"Кусков после разбиения: {len(all_chunks)}")

# 2) перевести все куски по одному с паузами
translated = []
for i, c in enumerate(all_chunks):
    t = translate_one(c)
    translated.append(t)
    status = "ok" if t else "ПУСТО"
    print(f"  [{i+1}/{len(all_chunks)}] {status}: {c[:30]} → {t[:50]}")
    time.sleep(PAUSE)

# 3) склеить по сегментам, сохранить в кэш
for i, s in enumerate(todo, 1):
    start, end = seg_ranges[i - 1]
    parts = [t for t in translated[start:end] if t]
    if not parts:
        print(f"[{i}/{len(todo)}] ПУСТО: {str(s['text'])[:40]}")
        continue
    cache[key(s)] = " ".join(parts)
    print(f"[{i}/{len(todo)}] {cache[key(s)][:70]}")

    if i % 10 == 0:
        json.dump({"translations": cache},
                  open(cache_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

json.dump({"translations": cache},
          open(cache_path, "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print(f"Готово: {cache_path}  (всего в кэше {len(cache)})")