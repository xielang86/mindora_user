"""Workbook-backed content metadata; exact mappings only, no name guessing."""
import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data/content_tags"
SOP_COMMANDS = {
    "1": "sleep.scene.cocos_island_moonlight", "2": "sleep.scene.amalfi_breeze",
    "3": "sleep.scene.kyoto_forest", "4": "sleep.scene.andaman_rainforest_sanctuary",
    "5": "sleep.scene.bhutan_misty_forest", "6": "sleep.scene.sedona_red_rock_peace",
    "7": "sleep.scene.fogo_island_cookie_box",
    "8": "sleep.scene.bhutan_snow_peak_chant", "9": "sleep.scene.taupo_misty_valley",
    "10": "sleep.scene.golden_dune_mirage",
}
AUDIO_COMMANDS = {
    "轻柔海浪": "sleep.pure_music.ocean_wave", "溪水": "sleep.pure_music.babbling_brook",
    "河流": "sleep.pure_music.river_sound", "春天鸟叫": "sleep.pure_music.birds_chirping",
    "江边篝火": "sleep.pure_music.campfire",
}
SOP_TAGS = {"D": "scene", "E": "scene_detail", "F": "culture", "G": "atmosphere",
            "K": "nature_sound", "L": "animal_sound", "M": "object_sound",
            "N": "instrument", "O": "music", "Q": "guidance", "R": "body",
            "S": "goal", "U": "risk", "X": "sound_class", "Y": "soundscape",
            "Z": "noise_approximation", "AA": "density_stimulation", "AB": "continuity"}
AUDIO_TAGS = {"F": "scene", "H": "sound_class", "I": "sound_detail", "K": "animal_sound",
              "L": "object_sound", "M": "music", "N": "noise_approximation",
              "O": "soundscape", "P": "continuity", "Q": "density", "R": "stimulation",
              "S": "atmosphere", "T": "goal", "X": "risk"}


def canonical_cmd(cmd):
    if cmd.startswith(("scene.", "pure_music.")):
        return "sleep." + cmd
    if not cmd.startswith("sleep."):
        return "sleep.scene." + cmd
    return cmd


def tags_for(cells, columns):
    # Split only explicit list delimiters; preserve slash phrases and uncertainty.
    return sorted({dimension + ":" + value.strip() for column, dimension in columns.items()
                   for value in cells.get(column, "").split("；") if value.strip()})


@lru_cache(maxsize=1)
def load_catalog():
    entries = []
    for filename, sheet_name, kind, columns in [
        ("sop_source.json", "02_sop内容分层标签", "sop", SOP_TAGS),
        ("audio_source.json", "02_自然音内容分级标签", "nature_audio", AUDIO_TAGS),
        ("audio_source.json", "03_轻唤醒内容分级标签", "wake_audio", {"F": "scene", "G": "sound_class", "H": "sound_detail", "I": "noise_approximation", "K": "stimulation", "L": "goal", "O": "risk"}),
    ]:
        book = json.loads((DATA / filename).read_text(encoding="utf-8"))
        sheet = next(s for s in book["sheets"] if s["name"] == sheet_name)
        headers = sheet["rows"][0]["cells"]
        for row in sheet["rows"][1:]:
            cells = row["cells"]
            command = SOP_COMMANDS.get(cells.get("A")) if kind == "sop" else AUDIO_COMMANDS.get(cells.get("B")) if kind == "nature_audio" else None
            entries.append({"content_id": kind + ":" + cells["A"], "kind": kind,
                            "name": cells["B"], "description": cells.get("C", ""),
                            "cmd_name": command, "mapping_status": "mapped" if command else "unmapped",
                            "tags": tags_for(cells, columns),
                            "attributes": {headers.get(k, k): v for k, v in cells.items()},
                            "source": {"file": book["source_file"], "sheet": sheet_name, "row": row["row"]}})
    book = json.loads((DATA / "sop_source.json").read_text(encoding="utf-8"))
    variants = next(s for s in book["sheets"] if s["name"] == "03_引导词标签")
    headers = variants["rows"][0]["cells"]
    for entry in entries:
        if entry["kind"] == "sop":
            entry["guide_variants"] = [{"variant": r["cells"]["C"],
                "attributes": {headers.get(k, k): v for k, v in r["cells"].items()},
                "source_row": r["row"]} for r in variants["rows"][1:]
                if "sop:" + r["cells"]["A"] == entry["content_id"]]
    return entries


def catalog_by_command():
    return {item["cmd_name"]: item for item in load_catalog() if item["cmd_name"]}


def candidate_metadata(commands):
    catalog = catalog_by_command()
    result = []
    for cmd in commands:
        entry = catalog.get(canonical_cmd(cmd))
        # Full original text/50 guide variants remain in catalog, not every LLM request.
        summary = {k: entry[k] for k in ("content_id", "name", "description", "tags", "source")} if entry else None
        result.append({"cmd_name": cmd, "content": summary})
    return result
