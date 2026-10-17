#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工地台账云端读写封装。

流水表 database_id : <你的流水表ID>
配置表 database_id : <你的配置表ID>
用法：
  python ledger.py add <json文件>          批量写入流水
  python ledger.py list [--type 开销] [--from 2026-10-01] [--to 2026-10-31]
  python ledger.py config                   读取名单/分类/设备
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

def _resolve_skill_dir() -> Path:
    """定位 library 技能的 database 子脚本目录（兼容不同安装环境与机器）。

    优先级：
      1. 应用内置目录 app.asar.unpacked（常见盘符 F:/ C:/ D:/）。
      2. 用户插件缓存 ~/.workbuddy/plugins/cache/workbuddy-builtin/<版本>/skills/library。
      3. 全盘 glob 兜底（覆盖自定义安装盘符）。
    返回的目录需包含 database/query_database_record.py 等脚本。
    """
    import glob as _glob
    roots = []
    for d in ("F:/workbuddy", "C:/workbuddy", "D:/workbuddy"):
        roots.append(Path(d) / "resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library")
    roots.append(Path.home() / ".workbuddy/plugins/cache/workbuddy-builtin")
    roots.append(Path.home() / ".workbuddy")
    for c in roots:
        if (c / "database" / "query_database_record.py").exists():
            return c
    # 用户插件缓存为嵌套版本目录：.../workbuddy-builtin/<ver>/skills/library
    pats = [
        str(Path.home() / ".workbuddy/plugins/cache/workbuddy-builtin/*/skills/library/database/query_database_record.py"),
        "C:/Users/*/.workbuddy/plugins/cache/workbuddy-builtin/*/skills/library/database/query_database_record.py",
        "F:/workbuddy/**/skills/library/database/query_database_record.py",
        "C:/workbuddy/**/skills/library/database/query_database_record.py",
        "D:/workbuddy/**/skills/library/database/query_database_record.py",
    ]
    for pat in pats:
        for hit in _glob.glob(pat, recursive=True):
            return Path(hit).parent.parent          # .../skills/library
    return Path("F:/workbuddy/resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library")


SKILL = _resolve_skill_dir()
FLOW_DB = "<你的流水表ID>"
CONF_DB = "<你的配置表ID>"
FLOW_FIELDS = ["日期", "类型", "人员", "分类", "金额", "工时", "数量", "单位",
               "对方", "项目", "地点", "设备", "欠款方向", "欠款状态",
               "工作内容", "备注", "原始记录", "录入时间", "录入来源",
               "原始凭证", "凭证说明", "图片识别文字", "确认状态", "存疑点"]
NUM_FIELDS = {"金额", "工时", "数量"}
SEL_FIELDS = {"类型", "欠款方向", "欠款状态", "录入来源", "确认状态"}
DATE_FIELDS = {"日期", "录入时间"}
EVIDENCE_FOLDER = "<你的凭证夹ID>"   # 资料库「工地凭证」文件夹
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska", ".3gp": "video/3gpp",
        ".pdf": "application/pdf", ".heic": "image/heic"}


def run(script: str, payload: dict) -> dict:
    p = subprocess.run(
        [sys.executable, str(SKILL / "database" / script), "--stdin"],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
    )
    out = p.stdout.decode("utf-8", "replace").strip()
    if not out:
        return {"error": p.stderr.decode("utf-8", "replace")[:400]}
    # stdout 可能包含多行（如 KS_USER_REPLY 回执），逐行找第一个合法 JSON
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {"raw": out}


def _wrap_value(field: str, value):
    """把扁平值转成 database 写入所需的 oneof 结构。"""
    if isinstance(value, dict):
        return value
    if field in DATE_FIELDS:
        return {"date": str(value)}
    if field in NUM_FIELDS:
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None
    if field in SEL_FIELDS:
        return {"select": str(value)}
    return {"text": "" if value is None else str(value)}


def _evidence_item(path: str) -> dict:
    """把本地凭证文件路径转成 AttachmentItem 结构。

    文件需先由调用方上传到资料库「工地凭证」文件夹，
    这里把上传返回的 url 与本地文件元信息组装成 attachment 元素。
    """
    f = Path(path)
    ext = f.suffix.lower()
    return {"name": f.name,
            "url": EVIDENCE_URL_MAP.get(str(f.resolve()), ""),
            "fileType": MIME.get(ext, "application/octet-stream"),
            "fileSize": f.stat().st_size,
            "mediaType": ("image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")
                          else "video" if ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp")
                          else "file")}


EVIDENCE_URL_MAP: dict = {}


def cmd_add(path: str) -> None:
    raw = Path(path).read_text(encoding="utf-8")
    items = json.loads(raw)
    if isinstance(items, dict):
        items = [items]
    records, skipped = [], []
    for it in items:
        props = {}
        for k, v in it.items():
            if k not in FLOW_FIELDS or v in (None, ""):
                continue
            if k == "原始凭证":
                # 凭证：value 为 [{name,url,fileType,fileSize,mediaType}] 或
                # 简写 [{ "本地路径": ..., "资料库链接": ... }]
                att = []
                for e in (v if isinstance(v, list) else [v]):
                    if "资料库链接" in e:
                        p = Path(e["本地路径"])
                        ext = p.suffix.lower()
                        att.append({"name": e.get("名称") or p.name,
                                    "url": e["资料库链接"],
                                    "fileType": MIME.get(ext, "application/octet-stream"),
                                    "fileSize": p.stat().st_size if p.exists() else 0,
                                    "mediaType": ("image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")
                                                  else "video" if ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp")
                                                  else "file")})
                    else:
                        att.append(e)
                props[k] = {"attachment": att}
                continue
            wrapped = _wrap_value(k, v)
            if wrapped:
                props[k] = wrapped
        if props:
            records.append(props)
        else:
            skipped.append(it)
    if not records:
        print(json.dumps({"error": "没有有效字段可写入"}, ensure_ascii=False))
        return
    # 每批最多 100 条
    total_ok, total_fail = 0, 0
    for i in range(0, len(records), 100):
        chunk = records[i:i + 100]
        res = run("batch_add_database_records.py",
                  {"database_id": FLOW_DB, "records": chunk})
        for r in res.get("results", []):
            if r.get("success"):
                total_ok += 1
            else:
                total_fail += 1
                print("失败：", r.get("error"), file=sys.stderr)
        if "error" in res:
            print("批次错误：", res["error"], file=sys.stderr)
            total_fail += len(chunk)
    out = {"written": total_ok, "failed": total_fail, "skipped": len(skipped)}
    print(json.dumps(out, ensure_ascii=False))


def cmd_list(args) -> None:
    payload = {"database_id": FLOW_DB, "page_size": 200,
               "sorts": [{"property": "日期", "direction": "descending"}]}
    if args.type:
        payload["filter"] = {"property": {
            "property": "类型", "select": {"equals": args.type}}}
    rows = []
    start_cursor = ""
    # 翻页：流水超过 200 条时按 next_cursor 持续拉取，避免静默丢数据
    while True:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        res = run("query_database_record.py", payload)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        start_cursor = res.get("next_cursor") or ""
        if not start_cursor:
            break

    def day(r):
        # 服务端日期返回 ISO 串，统一截取日期部分
        return (r.get("日期") or "")[:10]

    if args.frm:
        rows = [r for r in rows if day(r) >= args.frm]
    if args.to:
        rows = [r for r in rows if day(r) <= args.to]
    print(json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False))


def cmd_update(args) -> None:
    """按 record_id 更新字段。

    用法：
      python ledger.py update fix.json
    其中 fix.json 为记录数组：
      [{"record_id":"xxx","备注":"新备注","工时":10}, ...]
    字段值会自动包成 oneof 结构；未知字段会被跳过。
    """
    raw = Path(args.file).read_text(encoding="utf-8")
    items = json.loads(raw)
    if isinstance(items, dict):
        items = [items]
    recs = []
    for it in items:
        rid = it.get("record_id")
        if not rid:
            print("跳过：缺少 record_id ->", it, file=sys.stderr)
            continue
        props = {}
        for k, v in it.items():
            if k == "record_id" or v in (None, ""):
                continue
            if k not in FLOW_FIELDS:
                print("跳过未知字段：", k, file=sys.stderr)
                continue
            wrapped = _wrap_value(k, v)
            if wrapped:
                props[k] = wrapped
        if props:
            recs.append({"record_id": rid, "properties": props})
    if not recs:
        print(json.dumps({"error": "没有可更新的字段"}, ensure_ascii=False))
        return
    res = run("batch_update_database_records.py",
              {"database_id": FLOW_DB, "records": recs})
    print(json.dumps(res, ensure_ascii=False))


def cmd_delete(args) -> None:
    """按 record_id 批量删除（不可逆，慎用；删除前务必列清单经用户确认）。

    用法：
      python ledger.py delete del.json
    del.json 为 record_id 数组，或 {"record_ids":[...]}。
    """
    raw = Path(args.file).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict):
        ids = data.get("record_ids") or data.get("records") or []
    else:
        ids = data
    ids = [str(x).strip() for x in ids if str(x).strip()]
    if not ids:
        print(json.dumps({"error": "没有要删除的 record_id"}, ensure_ascii=False))
        return
    res = run("batch_delete_database_records.py",
              {"database_id": FLOW_DB, "record_ids": ids})
    print(json.dumps(res, ensure_ascii=False))


def cmd_config() -> None:
    res = run("query_database_record.py",
              {"database_id": CONF_DB, "page_size": 200})
    print(json.dumps({"rows": res.get("results", [])}, ensure_ascii=False))


def cmd_pending() -> None:
    """列出待确认 / 有疑问的记录，供确认流程使用。"""
    out = {}
    for state in ("待确认", "有疑问"):
        r = run("query_database_record.py",
                {"database_id": FLOW_DB, "page_size": 200,
                 "filter": {"property": {"property": "确认状态",
                                        "select": {"equals": state}}}})
        out[state] = r.get("results", [])
    print(json.dumps(out, ensure_ascii=False))


def cmd_confirm(args) -> None:
    """更新确认状态。

    用法：
      python ledger.py confirm <record_id> 已确认
      python ledger.py confirm <record_id> 有疑问 --doubt "金额看不清"
    """
    rec = {"record_id": args.record_id,
           "properties": {"确认状态": {"select": args.state}}}
    payload = {"database_id": FLOW_DB, "records": [rec]}
    if getattr(args, "doubt", None):
        rec["properties"]["存疑点"] = {"text": args.doubt}
    res = run("batch_update_database_records.py", payload)
    print(json.dumps(res, ensure_ascii=False))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "add":
        cmd_add(sys.argv[2])
    elif cmd == "pending":
        cmd_pending()
    elif cmd == "confirm":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("record_id")
        p.add_argument("state")
        p.add_argument("--doubt")
        cmd_confirm(p.parse_args(sys.argv[2:]))
    elif cmd == "list":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--type")
        p.add_argument("--from", dest="frm")
        p.add_argument("--to")
        cmd_list(p.parse_args(sys.argv[2:]))
    elif cmd == "config":
        cmd_config()
    elif cmd == "delete":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("file")
        cmd_delete(p.parse_args(sys.argv[2:]))
    elif cmd == "update":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("file")
        cmd_update(p.parse_args(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()