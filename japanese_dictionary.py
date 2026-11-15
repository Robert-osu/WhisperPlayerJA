
from functools import lru_cache

import pykakasi
from fugashi import Tagger
from jamdict import Jamdict


class JapaneseDictionary:
    """Локальный японский словарь на основе Fugashi и JMdict."""

    def __init__(self):
        self.tagger = Tagger()
        self.jmdict = Jamdict()
        self.kakasi = pykakasi.kakasi()

        if not self.jmdict.ready:
            raise RuntimeError("База JMdict не загружена")

    @staticmethod
    def _get_feature(feature, name, default=""):
        value = getattr(feature, name, None)
        return str(value) if value else default

    @staticmethod
    def _get_glosses(entry):
        meanings = []

        for sense in entry.senses:
            for gloss in getattr(sense, "gloss", []):
                value = getattr(gloss, "text", str(gloss))
                if value and value not in meanings:
                    meanings.append(value)

        return meanings

    @staticmethod
    def _get_pos(entry):
        result = []

        for sense in entry.senses:
            for pos in getattr(sense, "pos", []):
                value = str(pos)
                if value not in result:
                    result.append(value)

        return result

    @lru_cache(maxsize=4096)
    def lookup(self, token: str) -> dict:
        token = token.strip()

        if not token:
            return {}

        # Морфологический анализ
        nodes = list(self.tagger(token))

        if not nodes:
            return {}

        # Для первой версии рассматриваем основной токен.
        node = nodes[0]
        feature = node.feature

        surface = node.surface

        lemma = self._get_feature(
            feature, "lemma", surface
        )

        reading = self._get_feature(
            feature, "kana"
        )

        if not reading or reading == "*":
            reading = self._get_feature(
                feature, "pron", ""
            )

        pos = self._get_feature(
            feature, "pos1", "Неизвестно"
        )

        # Поиск по начальной форме
        result = self.jmdict.lookup(
            lemma,
            lookup_chars=False,
            lookup_ne=False
        )

        # Если лемма не найдена — ищем исходную форму
        if not result.entries and surface != lemma:
            result = self.jmdict.lookup(
                surface,
                lookup_chars=False,
                lookup_ne=False
            )

        entries = result.entries

        meanings = []
        dictionary_pos = []

        if entries:
            entry = entries[0]
            meanings = self._get_glosses(entry)
            dictionary_pos = self._get_pos(entry)

        # Конвертация чтения в хирагану и ромадзи
        if reading and reading != "*":
            converted = self.kakasi.convert(reading)

            hiragana = "".join(
                item["hira"] for item in converted
            )

            romaji = "".join(
                item["hepburn"] for item in converted
            )
        else:
            hiragana = ""
            romaji = ""

        return {
            "surface": token,
            "lemma": lemma,
            "phonetic": f"[{hiragana}]" if hiragana else "[ — ]",
            "translation": "; ".join(meanings[:5]) or "Перевод не найден",
            "pronunciation": romaji or "—",
            "part_of_speech": (
                ", ".join(dictionary_pos[:3])
                if dictionary_pos else pos
            ),
            "example": "—",
            "found": bool(entries),
        }


def rebuild_japanese_word_segments(segments, tagger):
    """
    Перестраивает японские word_segments:
    - текст берётся из segment['text'];
    - границы слов определяет Fugashi;
    - временные метки собираются из segment['words'].

    Если текст не удаётся точно сопоставить,
    оставляет исходные слова этого сегмента.
    """
    import unicodedata

    result = []

    for segment in segments:
        text = segment.get("text", "")
        source_words = segment.get("words", [])

        if not text or not source_words:
            result.extend(source_words)
            continue

        # Разбиваем текст на морфологические токены.
        tokens = [
            node.surface
            for node in tagger(text)
            if node.surface
        ]

        # Создаём последовательность символов с временными метками.
        timed_chars = []

        for item in source_words:
            word = item.get("word", "")

            for char in word:
                if char.isspace():
                    continue

                timed_chars.append({
                    "char": char,
                    "start": item["start"],
                    "end": item["end"],
                    "score": item.get("score", 0.0),
                })

        # Нормализуем только для проверки соответствия.
        def normalize(value):
            return "".join(
                unicodedata.normalize("NFKC", char)
                for char in value
                if not char.isspace()
            )

        source_text = normalize(text)
        timed_text = normalize(
            "".join(item["char"] for item in timed_chars)
        )
        token_text = normalize("".join(tokens))

        # Если данные расходятся, не рискуем синхронизацией.
        if source_text != timed_text or source_text != token_text:
            result.extend(source_words)
            continue

        position = 0

        for token in tokens:
            token_chars = [
                char for char in token if not char.isspace()
            ]

            if not token_chars:
                continue

            count = len(token_chars)
            matched = timed_chars[position:position + count]

            if len(matched) != count:
                break

            result.append({
                "word": token,
                "start": matched[0]["start"],
                "end": matched[-1]["end"],
                "score": (
                    sum(item["score"] for item in matched) / count
                ),
            })

            position += count

    return result
