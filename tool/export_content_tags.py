"""Export source workbooks losslessly to JSON and readable Markdown (stdlib only)."""
import json
import posixpath
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_workbook(path):
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(t.text or "" for t in si.findall(".//s:t", NS))
                       for si in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        relationships = {r.attrib["Id"]: r.attrib["Target"] for r in
                         ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        sheets = []
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS):
            target = relationships[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            xml = ET.fromstring(archive.read(target))
            rows = []
            for row in xml.findall("s:sheetData/s:row", NS):
                cells = {}
                for cell in row.findall("s:c", NS):
                    value = cell.find("s:v", NS)
                    text = value.text or "" if value is not None else "".join(t.text or "" for t in cell.findall(".//s:t", NS))
                    if cell.get("t") == "s":
                        text = strings[int(text)]
                    if text:
                        cells[''.join(c for c in cell.attrib['r'] if c.isalpha())] = text
                if cells:
                    rows.append({"row": int(row.attrib["r"]), "cells": cells})
            sheets.append({"name": sheet.attrib["name"], "rows": rows,
                           "merged_cells": [c.attrib["ref"] for c in xml.findall("s:mergeCells/s:mergeCell", NS)]})
        return {"schema_version": 1, "source_file": path.name, "sheets": sheets}


def export():
    output = ROOT / "data/content_tags"
    output.mkdir(parents=True, exist_ok=True)
    for filename, stem in [("sop内容标签对照表.xlsx", "sop_source"),
                           ("Mindora_自然音&闹钟音频内容标签分级_v1 .xlsx", "audio_source")]:
        book = read_workbook(ROOT / filename)
        (output / (stem + ".json")).write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["# " + filename, "", "由 tool/export_content_tags.py 生成；原文保留，不代表已验证的健康效果。", ""]
        for sheet in book["sheets"]:
            lines += ["## " + sheet["name"], ""]
            for row in sheet["rows"]:
                lines += [f"- 第 {row['row']} 行：" + "； ".join(k + ": " + v.replace("\n", " / ") for k, v in row["cells"].items())]
            lines.append("")
        (output / (stem + ".md")).write_text("\n".join(lines), encoding="utf-8")
        print(stem, [(s["name"], len(s["rows"])) for s in book["sheets"]])

    sys.path.insert(0, str(ROOT))
    from content_tags import load_catalog
    load_catalog.cache_clear()
    (output / "catalog.json").write_text(json.dumps({"schema_version": 1,
        "contents": load_catalog()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    export()
