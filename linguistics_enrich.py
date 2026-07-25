#!/usr/bin/env python3
"""
Enrich existing linguistics KB language records with data from:
  - WALS    (word order, morphology type, 9 typological features)
  - PHOIBLE (phoneme/consonant/vowel counts, tonal flag, sample inventory)
  - Glottolog (glottocode, macroarea, lat/lon)
  - ASJP    (Swadesh item count, asjp_id)

Matching is by ISO-639-3 code first, normalized name fallback.
Only writes a file if it actually changed.
"""

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

KB_LANGUAGE = Path("D:/Runtime/kb/linguistics/entries/language")
WALS_DIR    = Path("D:/Language Source/wals/cldf")
PHOIBLE_CSV = Path("D:/Language Source/phoible/data/phoible.csv")
GLOTTO_CSV  = Path("D:/Language Source/glottolog/cldf/languages.csv")
ASJP_LANGS  = Path("D:/Language Source/asjp/cldf/languages.csv")
ASJP_FORMS  = Path("D:/Language Source/asjp/cldf/forms.csv")

# WALS parameter IDs to pull and which KB field they map to
WALS_PARAMS = {
    "81A": "word_order",            # Order of Subject, Object, and Verb
    "26A": "morphology_type",       # Prefixing vs. Suffixing in Inflectional Morphology
    "1A":  "consonant_inventory_size",
    "2A":  "vowel_quality_size",
    "3A":  "consonant_vowel_ratio",
    "30A": "gender_count",
    "37A": "definite_articles",
    "38A": "indefinite_articles",
    "83A": "adj_noun_order",
    "86A": "genitive_noun_order",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_wals():
    iso_to_id: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    with open(WALS_DIR / "languages.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wid = row["ID"].strip()
            iso = row["ISO639P3code"].strip()
            if iso:
                iso_to_id[iso] = wid
            name_to_id[slug(row["Name"].strip())] = wid

    code_label: dict[str, str] = {}
    with open(WALS_DIR / "codes.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code_label[row["ID"]] = row["Name"]

    features: dict[str, dict] = defaultdict(dict)
    with open(WALS_DIR / "values.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = row["Parameter_ID"]
            if pid not in WALS_PARAMS:
                continue
            lid = row["Language_ID"]
            features[lid][pid] = code_label.get(row["Code_ID"], row["Value"])

    return iso_to_id, name_to_id, features


def load_phoible():
    inv: dict[str, dict] = defaultdict(
        lambda: {"phones": set(), "cons": set(), "vow": set(), "tonal": False}
    )
    with open(PHOIBLE_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iso = row["ISO6393"].strip()
            if not iso or iso == "NA":
                continue
            phone = row["Phoneme"].strip()
            cls   = row["SegmentClass"].strip()
            tone  = row.get("tone", "0").strip()
            inv[iso]["phones"].add(phone)
            if cls == "consonant":
                inv[iso]["cons"].add(phone)
            elif cls == "vowel":
                inv[iso]["vow"].add(phone)
            if tone in ("+", "1"):
                inv[iso]["tonal"] = True

    out: dict[str, dict] = {}
    for iso, d in inv.items():
        out[iso] = {
            "phoneme_count":    len(d["phones"]),
            "consonant_count":  len(d["cons"]),
            "vowel_count":      len(d["vow"]),
            "is_tonal":         d["tonal"],
            "phonemes_sample":  sorted(d["phones"])[:20],
        }
    return out


def load_glottolog():
    iso_map:  dict[str, dict] = {}
    name_map: dict[str, dict] = {}
    with open(GLOTTO_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            entry = {
                "glottocode": row.get("ID", "").strip() or row.get("Glottocode", "").strip(),
                "macroarea":  row.get("Macroarea", "").strip(),
                "latitude":   row.get("Latitude", "").strip(),
                "longitude":  row.get("Longitude", "").strip(),
            }
            iso = row.get("ISO639P3code", "").strip()
            if iso:
                iso_map[iso] = entry
            name = row.get("Name", "").strip()
            if name:
                name_map[slug(name)] = entry
    return iso_map, name_map


def load_asjp():
    iso_map:  dict[str, str] = {}
    name_map: dict[str, str] = {}
    with open(ASJP_LANGS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lid = row.get("ID", "").strip()
            iso = row.get("ISO639P3code", "").strip()
            name = row.get("Name", "").strip()
            if iso:
                iso_map[iso] = lid
            if name:
                name_map[slug(name)] = lid

    form_counts: dict[str, int] = defaultdict(int)
    with open(ASJP_FORMS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lid = row.get("Language_ID", "").strip()
            if lid:
                form_counts[lid] += 1

    return iso_map, name_map, form_counts


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def enrich(record: dict,
           wals_iso, wals_name, wals_feats,
           phoible,
           glotto_iso, glotto_name,
           asjp_iso, asjp_name, asjp_forms) -> bool:
    iso      = (record.get("iso_code") or "").strip()
    name_key = slug(record.get("name") or "")
    changed  = False

    def set_if_missing(key, val):
        nonlocal changed
        if not record.get(key) and val not in (None, "", [], {}):
            record[key] = val
            changed = True

    def set_always(key, val):
        nonlocal changed
        if val not in (None, "", [], {}) and record.get(key) != val:
            record[key] = val
            changed = True

    # WALS
    wid = wals_iso.get(iso) or wals_name.get(name_key)
    if wid:
        set_always("wals_id", wid)
        feats = wals_feats.get(wid, {})
        for pid, field in WALS_PARAMS.items():
            val = feats.get(pid)
            if val:
                if field in ("word_order", "morphology_type"):
                    set_if_missing(field, val)  # don't overwrite hand-curated values
                else:
                    set_always(field, val)

    # PHOIBLE
    phon = phoible.get(iso)
    if phon:
        set_always("phoneme_count",   phon["phoneme_count"])
        set_always("consonant_count", phon["consonant_count"])
        set_always("vowel_count",     phon["vowel_count"])
        set_always("is_tonal",        phon["is_tonal"])
        set_always("phonemes_sample", phon["phonemes_sample"])

    # Glottolog
    geo = glotto_iso.get(iso) or glotto_name.get(name_key)
    if geo:
        set_if_missing("glottocode", geo["glottocode"])
        set_if_missing("macroarea",  geo["macroarea"])
        set_if_missing("latitude",   geo["latitude"])
        set_if_missing("longitude",  geo["longitude"])

    # ASJP
    aid = asjp_iso.get(iso) or asjp_name.get(name_key)
    if aid:
        count = asjp_forms.get(aid, 0)
        if count:
            set_always("swadesh_items", count)
            set_if_missing("asjp_id", aid)

    return changed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading WALS …")
    wals_iso, wals_name, wals_feats = load_wals()
    print(f"  {len(wals_iso)} ISO codes, {len(wals_feats)} languages with features")

    print("Loading PHOIBLE …")
    phoible = load_phoible()
    print(f"  {len(phoible)} languages with phoneme inventories")

    print("Loading Glottolog …")
    glotto_iso, glotto_name = load_glottolog()
    print(f"  {len(glotto_iso)} ISO-mapped languoids")

    print("Loading ASJP …")
    asjp_iso, asjp_name, asjp_forms = load_asjp()
    print(f"  {len(asjp_iso)} ISO-mapped languages, {len(asjp_forms)} with word lists")

    entries = sorted(p for p in KB_LANGUAGE.glob("*.json") if not p.name.startswith("_"))
    print(f"\nProcessing {len(entries)} language records …\n")

    updated = 0
    no_match = 0

    for path in entries:
        try:
            text = path.read_text(encoding="utf-8")
            record = json.loads(text)
        except Exception as e:
            print(f"  SKIP {path.name}: {e}")
            continue

        changed = enrich(
            record,
            wals_iso, wals_name, wals_feats,
            phoible,
            glotto_iso, glotto_name,
            asjp_iso, asjp_name, asjp_forms,
        )

        if changed:
            path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            updated += 1
        else:
            no_match += 1

    print(f"Done.  {updated} records enriched,  {no_match} had no matches.")


if __name__ == "__main__":
    main()
