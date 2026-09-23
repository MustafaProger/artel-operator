"""Stable Russian alphabetical ordering, independent of the host locale."""
import re
import unicodedata


def alphabet_key(value):
    name = unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е")
    return re.sub(r"[^\w\s-]", "", name).strip()
