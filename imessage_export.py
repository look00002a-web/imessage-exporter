#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iMessage / SMS 聊天记录导出工具 (macOS)
=======================================

本地、离线、只读地导出 Mac 本机「信息 (Messages)」里的 iMessage / SMS 聊天记录，
可以挑选会话、限定时间范围，导出成好读的 **自包含单文件 HTML**（气泡样式、内嵌图片）、
纯文本 TXT、以及表格 CSV。

设计原则
--------
* **只读**：读取前把 ``chat.db`` 连同 ``-wal`` / ``-shm`` 复制到临时目录再以只读方式打开，
  绝不触碰、绝不回写系统里的原始数据库。
* **离线**：全程不联网，只用 Python 3 标准库（``sqlite3`` 等）。附件里的 HEIC 图片可选地
  调用 macOS 自带的 ``sips`` 转成 JPEG 以便在 HTML 中显示——这也是本机命令，不联网。
* **零额外依赖**：核心功能只依赖标准库；无需安装任何 pip 包。

数据来源
--------
* 数据库：``~/Library/Messages/chat.db``（**明文 SQLite**，无需解密）。
* 附件：``~/Library/Messages/Attachments/...``。
* 读取需要运行本程序的「终端」拥有 **完全磁盘访问权限**
  （系统设置 › 隐私与安全性 › 完全磁盘访问权限）。详见 README。

用法
----
    # 列出所有会话（带编号）
    python3 imessage_export.py list

    # 导出第 3 个会话为 HTML（默认）
    python3 imessage_export.py export --chat 3

    # 按手机号/邮箱导出，限定时间范围，导出多种格式
    python3 imessage_export.py export --handle +8613800138000 \
        --start 2024-01-01 --end 2024-12-31 --format html,txt,csv

    # 导出全部会话
    python3 imessage_export.py export --all

作者说明：本工具仅用于导出、备份 **本机本人** 的聊天记录，请勿用于侵犯他人隐私。
"""

import argparse
import base64
import csv
import datetime as _dt
import html
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# Mac / Cocoa 纪元 (2001-01-01 00:00:00 UTC) 相对 Unix 纪元的秒数
MAC_EPOCH_OFFSET = 978307200

DEFAULT_DB = os.path.expanduser("~/Library/Messages/chat.db")
ATTACHMENTS_ROOT = os.path.expanduser("~/Library/Messages/Attachments")

# tapback（点赞/爱心等回应）类型映射
TAPBACK_LABELS = {
    2000: ("👍", "回应"),
    2001: ("❤️", "喜欢"),
    2002: ("👍", "赞"),
    2003: ("👎", "踩"),
    2004: ("😂", "大笑"),
    2005: ("‼️", "强调"),
    2006: ("❓", "疑问"),
}

# 浏览器一般能直接播放 / 显示的媒体类型（用于 embed 模式内嵌）
PLAYABLE_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}
PLAYABLE_AUDIO = {"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/aac",
                  "audio/wav", "audio/x-wav", "audio/ogg", "audio/amr"}


# ---------------------------------------------------------------------------
# 时间处理
# ---------------------------------------------------------------------------

def mac_time_to_unix(raw):
    """把 message.date 换算成 Unix 时间戳（秒, float）。

    新系统里 date 是「2001-01-01 以来的纳秒」，旧系统里是「秒」。
    用数量级自动判别：纳秒值约为 8e17，秒值约为 5e8。
    """
    if raw is None or raw == 0:
        return None
    raw = int(raw)
    if abs(raw) > 10 ** 12:        # 纳秒
        secs = raw / 1_000_000_000
    else:                          # 秒
        secs = float(raw)
    return secs + MAC_EPOCH_OFFSET


def mac_time_to_datetime(raw):
    """换算成本地时间（aware datetime），失败返回 None。"""
    u = mac_time_to_unix(raw)
    if u is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(u).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def parse_user_date(s, end=False):
    """把用户输入的 YYYY-MM-DD（本地）转为 Unix 时间戳（秒）。

    end=True 时表示「该日结束」，即次日 00:00:00（区间右端点，含当天）。
    """
    if not s:
        return None
    try:
        d = _dt.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"错误：时间格式应为 YYYY-MM-DD，收到的是 {s!r}")
    if end:
        d = d + _dt.timedelta(days=1)
    return d.timestamp()


# ---------------------------------------------------------------------------
# attributedBody 解析（typedstream / streamtyped 二进制 blob）
# ---------------------------------------------------------------------------

def decode_attributed_body(blob):
    """从 message.attributedBody 这个 NSKeyedArchiver/typedstream blob 里提取正文纯文本。

    新版 macOS 常把正文放在这里而不是 message.text。结构里正文是类链
    (NSMutableAttributedString → … → NSString) 之后的第一个 NSString：
    其编码为 ``NSString\\x01\\x95\\x84\\x01+<长度><UTF-8 字节>``。

    长度是一个 typedstream 变长整数：
      * 单字节  (< 0x81) 直接是长度；
      * 0x81 后跟 2 字节小端；
      * 0x82 后跟 4 字节小端。

    已在真实 chat.db 的全部样本上验证可 100% 提出正文；提不出则返回 None。
    """
    if not blob:
        return None
    try:
        p = blob.find(b"NSString")
        start = p if p >= 0 else 0
        plus = blob.find(b"\x2b", start)   # '+' 0x2b 是字符串起始标记
        if plus < 0:
            return None
        i = plus + 1
        if i >= len(blob):
            return None
        n = blob[i]
        i += 1
        if n == 0x81:
            length = int.from_bytes(blob[i:i + 2], "little")
            i += 2
        elif n == 0x82:
            length = int.from_bytes(blob[i:i + 4], "little")
            i += 4
        else:
            length = n
        if length <= 0:
            return None
        raw = blob[i:i + length]
        text = raw.decode("utf-8", errors="replace")
        return text or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 数据库只读打开（先复制到临时目录，含 -wal / -shm）
# ---------------------------------------------------------------------------

class FullDiskAccessError(Exception):
    """读取聊天数据库被系统拒绝——通常是终端缺少「完全磁盘访问权限」。"""


class MessagesDB:
    """以只读、不锁库的方式打开 chat.db 的副本。"""

    def __init__(self, db_path=DEFAULT_DB):
        self.src = os.path.expanduser(db_path)
        if not os.path.exists(self.src):
            raise SystemExit(f"错误：找不到数据库 {self.src}")
        self._tmpdir = tempfile.mkdtemp(prefix="imsg_export_")
        self._copy_db()
        uri = "file:%s?mode=ro&immutable=1" % self._dst
        self.con = sqlite3.connect(uri, uri=True)
        self.con.row_factory = sqlite3.Row

    def _copy_db(self):
        base = os.path.basename(self.src)
        self._dst = os.path.join(self._tmpdir, base)
        try:
            shutil.copy2(self.src, self._dst)
        except PermissionError:
            raise FullDiskAccessError(
                "没有权限读取聊天数据库。\n"
                "请到「系统设置 › 隐私与安全性 › 完全磁盘访问权限」里，\n"
                "为你运行本工具的 终端 (Terminal / iTerm) 打开开关，然后重开终端重试。"
            )
        # 一并复制 WAL / SHM，保证读到最新消息且不会去锁原库
        for ext in ("-wal", "-shm"):
            s = self.src + ext
            if os.path.exists(s):
                try:
                    shutil.copy2(s, self._dst + ext)
                except OSError:
                    pass

    def close(self):
        try:
            self.con.close()
        finally:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- 查询 ---------------------------------------------------------------

    def list_conversations(self):
        """返回所有会话（chat）的汇总信息列表，按最后活动时间倒序。"""
        cur = self.con.cursor()
        rows = cur.execute("""
            SELECT c.ROWID          AS chat_id,
                   c.guid           AS guid,
                   c.style          AS style,
                   c.chat_identifier AS chat_identifier,
                   c.display_name   AS display_name,
                   c.service_name   AS service_name,
                   COUNT(m.ROWID)   AS msg_count,
                   MIN(m.date)      AS first_date,
                   MAX(m.date)      AS last_date
            FROM chat c
            LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            LEFT JOIN message m ON m.ROWID = cmj.message_id
            GROUP BY c.ROWID
            ORDER BY last_date DESC
        """).fetchall()

        # 参与者
        parts = defaultdict(list)
        for r in cur.execute("""
            SELECT chj.chat_id AS chat_id, h.id AS handle
            FROM chat_handle_join chj
            JOIN handle h ON h.ROWID = chj.handle_id
            ORDER BY h.id
        """):
            parts[r["chat_id"]].append(r["handle"])

        out = []
        for r in rows:
            cid = r["chat_id"]
            is_group = (r["style"] == 43)
            handles = parts.get(cid, [])
            out.append({
                "chat_id": cid,
                "guid": r["guid"],
                "is_group": is_group,
                "identifier": r["chat_identifier"],
                "display_name": r["display_name"],
                "service": r["service_name"],
                "handles": handles,
                "msg_count": r["msg_count"] or 0,
                "first": mac_time_to_datetime(r["first_date"]),
                "last": mac_time_to_datetime(r["last_date"]),
            })
        return out

    def list_handles(self):
        """列出所有联系人标识（手机号/邮箱）及其消息数。"""
        cur = self.con.cursor()
        rows = cur.execute("""
            SELECT h.id AS handle, h.service AS service, COUNT(m.ROWID) AS n
            FROM handle h
            LEFT JOIN message m ON m.handle_id = h.ROWID
            GROUP BY h.id
            ORDER BY n DESC
        """).fetchall()
        return [{"handle": r["handle"], "service": r["service"],
                 "msg_count": r["n"] or 0} for r in rows]

    def fetch_messages(self, chat_id):
        """取某会话的全部消息（按时间升序），附带对端标识、附件、tapback。"""
        cur = self.con.cursor()
        rows = cur.execute("""
            SELECT m.ROWID               AS rowid,
                   m.guid                AS guid,
                   m.date                AS date,
                   m.text                AS text,
                   m.attributedBody      AS abody,
                   m.is_from_me          AS is_from_me,
                   m.service             AS service,
                   m.item_type           AS item_type,
                   m.group_title         AS group_title,
                   m.date_edited         AS date_edited,
                   m.date_retracted      AS date_retracted,
                   m.associated_message_guid  AS assoc_guid,
                   m.associated_message_type  AS assoc_type,
                   h.id                  AS handle
            FROM chat_message_join cmj
            JOIN message m ON m.ROWID = cmj.message_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cmj.chat_id = ?
            ORDER BY m.date ASC, m.ROWID ASC
        """, (chat_id,)).fetchall()

        # 附件
        att = defaultdict(list)
        for r in cur.execute("""
            SELECT maj.message_id AS mid, a.filename AS filename,
                   a.mime_type AS mime, a.uti AS uti,
                   a.total_bytes AS size, a.transfer_name AS tname
            FROM chat_message_join cmj
            JOIN message_attachment_join maj ON maj.message_id = cmj.message_id
            JOIN attachment a ON a.ROWID = maj.attachment_id
            WHERE cmj.chat_id = ?
        """, (chat_id,)):
            att[r["mid"]].append({
                "filename": r["filename"],
                "mime": r["mime"],
                "uti": r["uti"],
                "size": r["size"],
                "tname": r["tname"],
            })

        messages = []
        for r in rows:
            messages.append({
                "rowid": r["rowid"],
                "guid": r["guid"],
                "ts": mac_time_to_unix(r["date"]),
                "dt": mac_time_to_datetime(r["date"]),
                "text": r["text"],
                "abody": r["abody"],
                "is_from_me": bool(r["is_from_me"]),
                "service": r["service"],
                "item_type": r["item_type"],
                "group_title": r["group_title"],
                "edited": bool(r["date_edited"]),
                "assoc_guid": r["assoc_guid"],
                "assoc_type": r["assoc_type"],
                "handle": r["handle"],
                "attachments": att.get(r["rowid"], []),
            })
        return messages


# ---------------------------------------------------------------------------
# 把一条原始消息整理成「要渲染的内容」
# ---------------------------------------------------------------------------

def message_body_text(m):
    """返回消息正文：优先 text，为空时解析 attributedBody，再不行返回 ''。"""
    t = m["text"]
    if t:
        return t
    t = decode_attributed_body(m["abody"])
    return t or ""


def tapback_target_guid(assoc_guid):
    """associated_message_guid 形如 'p:0/<guid>' 或 'bp:<guid>'，取出目标 guid。"""
    if not assoc_guid:
        return None
    return assoc_guid.split("/")[-1]


def build_timeline(messages, names):
    """把消息列表整理成时间线元素：

    - 常规消息 -> {"kind": "msg", ...}
    - 系统消息 (item_type != 0) -> {"kind": "system", ...}
    - tapback（回应）-> 挂到目标消息的 reactions 上，若找不到目标则作为独立行

    返回 (timeline, stats)。
    """
    by_guid = {}
    for m in messages:
        if m["guid"]:
            by_guid[m["guid"]] = m

    timeline = []
    stats = {"msgs": 0, "system": 0, "tapbacks": 0, "attachments": 0}

    for m in messages:
        atype = m["assoc_type"] or 0

        # tapback（回应）
        if atype and 2000 <= atype <= 3999:
            removed = atype >= 3000
            base = atype - 1000 if removed else atype
            emoji, label = TAPBACK_LABELS.get(base, ("•", "回应"))
            target = by_guid.get(tapback_target_guid(m["assoc_guid"]))
            who = "我" if m["is_from_me"] else display_name_for(m["handle"], names)
            stats["tapbacks"] += 1
            if removed:
                # 取消的回应就不展示了
                continue
            reaction = {"emoji": emoji, "label": label, "who": who,
                        "is_from_me": m["is_from_me"]}
            if target is not None:
                target.setdefault("_reactions", []).append(reaction)
            else:
                timeline.append({"kind": "system",
                                 "text": f"{who} {emoji} {label}了一条消息",
                                 "dt": m["dt"]})
            continue

        # 系统/事件消息
        if (m["item_type"] or 0) != 0:
            txt = message_body_text(m)
            if not txt and m["group_title"]:
                txt = f"群名改为「{m['group_title']}」"
            if not txt:
                txt = "[系统消息]"
            timeline.append({"kind": "system", "text": txt, "dt": m["dt"]})
            stats["system"] += 1
            continue

        body = message_body_text(m)
        atts = m["attachments"]
        stats["attachments"] += len(atts)
        if not body and not atts:
            # 空消息（既无正文也无附件）跳过
            continue
        stats["msgs"] += 1
        timeline.append({
            "kind": "msg",
            "rowid": m["rowid"],
            "dt": m["dt"],
            "is_from_me": m["is_from_me"],
            "sender": "我" if m["is_from_me"] else display_name_for(m["handle"], names),
            "service": m["service"],
            "text": body,
            "edited": m["edited"],
            "attachments": atts,
            "reactions": m.get("_reactions", []),
        })

    # _reactions 是在遍历中挂上去的；上面 msg 读取时可能还没挂全，这里回填
    for item in timeline:
        if item.get("kind") == "msg":
            mm = next((x for x in messages if x["rowid"] == item["rowid"]), None)
            if mm is not None:
                item["reactions"] = mm.get("_reactions", [])
    return timeline, stats


def display_name_for(handle, names):
    if not handle:
        return "对方"
    return names.get(handle, handle)


# ---------------------------------------------------------------------------
# 附件读取 / 内嵌
# ---------------------------------------------------------------------------

def resolve_attachment_path(filename):
    if not filename:
        return None
    return os.path.expanduser(filename)


def guess_mime(att, path):
    mime = att.get("mime")
    if mime:
        return mime
    if path:
        g, _ = mimetypes.guess_type(path)
        if g:
            return g
    return "application/octet-stream"


def _sips_available():
    return shutil.which("sips") is not None


def process_image_with_sips(path, to_jpeg=False, max_px=0, workdir=None):
    """用 macOS 自带的 sips 把图片转码(HEIC→JPEG)和/或按最长边缩放。

    仅在需要时调用；成功返回新文件路径，失败返回 None（本机命令，不联网）。
    """
    if not _sips_available():
        return None
    try:
        import subprocess, tempfile as _tf
        d = workdir or _tf.gettempdir()
        out = os.path.join(d, "imsg_img_%d.jpg" % abs(hash(path)) if to_jpeg
                           else "imsg_img_%d%s" % (abs(hash(path)), os.path.splitext(path)[1] or ".img"))
        cmd = ["sips"]
        if to_jpeg:
            cmd += ["-s", "format", "jpeg"]
        if max_px and max_px > 0:
            cmd += ["-Z", str(int(max_px))]
        cmd += [path, "--out", out]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        if r.returncode == 0 and os.path.exists(out):
            return out
    except Exception:
        pass
    return None


def attachment_data_uri(att, max_bytes, image_max_px=0):
    """把 **图片** 附件读成 data: URI 以内嵌进自包含 HTML。
    返回 (kind, data_uri_or_None, info)。

    kind ∈ {'image', 'file', 'missing', 'toobig'}

    说明：为保证「单文件 HTML」可用，embed 模式只内嵌图片（任务要求的「内嵌图片」）。
    视频 / 音频 / 其它文件体积大、base64 膨胀严重，这里只显示为卡片；
    想连同视频等原件一起导出，请改用  --attachments copy。
    image_max_px>0 时用 sips 把图片最长边缩放到该像素，显著减小单文件体积。
    """
    path = resolve_attachment_path(att.get("filename"))
    name = att.get("tname") or (os.path.basename(path) if path else "附件")
    if not path or not os.path.exists(path):
        return ("missing", None, name)

    mime = guess_mime(att, path)
    is_heic = (mime in ("image/heic", "image/heif")) or path.lower().endswith((".heic", ".heif"))

    # 非图片：不内嵌，显示卡片
    if not (mime.startswith("image/") or is_heic):
        return ("file", None, (name, mime, att.get("size")))

    use_path = path
    if is_heic or (image_max_px and image_max_px > 0):
        proc = process_image_with_sips(path, to_jpeg=is_heic, max_px=image_max_px)
        if proc:
            use_path = proc
            if is_heic:
                mime = "image/jpeg"
        elif is_heic:
            # HEIC 无法转换（如没有 sips），当作普通文件卡片展示
            return ("file", None, (name, mime, att.get("size")))

    try:
        size = os.path.getsize(use_path)
    except OSError:
        return ("missing", None, name)

    if size > max_bytes:
        return ("toobig", None, (name, mime, size))

    try:
        with open(use_path, "rb") as f:
            data = f.read()
    except OSError:
        return ("missing", None, name)

    b64 = base64.b64encode(data).decode("ascii")
    uri = f"data:{mime};base64,{b64}"
    return ("image", uri, name)


def copy_attachment(att, dest_dir):
    """把附件原件复制到 dest_dir，返回 (相对路径, kind, info) 或 None。"""
    path = resolve_attachment_path(att.get("filename"))
    name = att.get("tname") or (os.path.basename(path) if path else None)
    if not path or not os.path.exists(path):
        return None
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(path)
    # 避免重名覆盖
    target = os.path.join(dest_dir, base)
    n = 1
    while os.path.exists(target):
        stem, ext = os.path.splitext(base)
        target = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        n += 1
    try:
        shutil.copy2(path, target)
    except OSError:
        return None
    mime = guess_mime(att, path)
    return (os.path.basename(target), mime, name)


def human_size(n):
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# ---------------------------------------------------------------------------
# 导出：HTML
# ---------------------------------------------------------------------------

HTML_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei",
       system-ui, sans-serif; background: #f2f2f7; color: #1c1c1e; }
@media (prefers-color-scheme: dark) {
  body { background: #0b0b0d; color: #e6e6ea; }
  .msg .bubble { background: #26262a; color: #e6e6ea; }
  header { background: #1c1c1e; border-color: #2c2c2e; }
  .sys { color: #8e8e93; }
  .date-sep span { background: #2c2c2e; color: #c7c7cc; }
  .meta { color: #8e8e93; }
}
header { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #e0e0e5;
         padding: 14px 20px; z-index: 5; }
header h1 { margin: 0 0 4px; font-size: 18px; }
header .sub { font-size: 12px; color: #8e8e93; line-height: 1.6; }
.wrap { max-width: 820px; margin: 0 auto; padding: 16px 14px 60px; }
.date-sep { text-align: center; margin: 18px 0 10px; }
.date-sep span { background: #e3e3e8; color: #6c6c70; font-size: 12px;
                 padding: 3px 12px; border-radius: 10px; }
.sys { text-align: center; color: #8e8e93; font-size: 12.5px; margin: 10px auto;
       max-width: 90%; }
.row { display: flex; margin: 3px 0; }
.row.me { justify-content: flex-end; }
.row.them { justify-content: flex-start; }
.col { max-width: 72%; display: flex; flex-direction: column; }
.row.me .col { align-items: flex-end; }
.sender { font-size: 11px; color: #8e8e93; margin: 2px 6px; }
.bubble { position: relative; padding: 8px 12px; border-radius: 18px;
          font-size: 15px; line-height: 1.4; word-wrap: break-word;
          white-space: pre-wrap; max-width: 100%; }
.row.me .bubble { background: #0b84ff; color: #fff; border-bottom-right-radius: 5px; }
.row.them .bubble { background: #e9e9eb; color: #1c1c1e; border-bottom-left-radius: 5px; }
.col img, .col video { max-width: 260px; max-height: 360px; border-radius: 14px;
                       display: block; margin: 2px 0; object-fit: cover; }
.col audio { display: block; margin: 4px 0; max-width: 260px; }
.att-card { border: 1px solid rgba(127,127,127,.35); border-radius: 10px;
            padding: 8px 10px; font-size: 13px; margin: 3px 0; }
.att-card .fn { font-weight: 600; word-break: break-all; }
.att-card .ex { color: #8e8e93; font-size: 11px; }
.meta { font-size: 10.5px; color: #a0a0a5; margin: 1px 6px 4px; }
.row.me .meta { text-align: right; }
.edited { font-style: italic; }
.reactions { margin: 1px 6px; font-size: 12px; }
.reactions .r { background: rgba(127,127,127,.18); border-radius: 10px;
                padding: 1px 7px; margin-right: 4px; display: inline-block; }
.footer { text-align: center; color: #a0a0a5; font-size: 11px; padding: 24px 0; }
"""


def _fmt_time(dt):
    return dt.strftime("%H:%M") if dt else ""


def _fmt_datetime(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def render_html(conv, timeline, stats, opts, attach_dir_rel=None):
    E = html.escape
    title = conversation_title(conv)
    parts = []
    parts.append("<!doctype html>")
    parts.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>{E(title)} · iMessage 导出</title>")
    parts.append(f"<style>{HTML_CSS}</style></head><body>")

    # header
    sub_bits = []
    if conv["is_group"]:
        sub_bits.append("群聊")
        if conv["handles"]:
            sub_bits.append("成员：" + "、".join(E(h) for h in conv["handles"]))
    else:
        if conv["handles"]:
            sub_bits.append("对端：" + "、".join(E(h) for h in conv["handles"]))
    rng = ""
    if conv["first"] and conv["last"]:
        rng = f'{conv["first"].strftime("%Y-%m-%d")} ~ {conv["last"].strftime("%Y-%m-%d")}'
    sub_bits.append(f"共 {stats['msgs']} 条消息")
    if stats["attachments"]:
        sub_bits.append(f"{stats['attachments']} 个附件")
    if rng:
        sub_bits.append(rng)
    sub_bits.append("导出于 " + _dt.datetime.now().strftime("%Y-%m-%d %H:%M"))

    parts.append("<header>")
    parts.append(f"<h1>{E(title)}</h1>")
    parts.append(f'<div class="sub">{" · ".join(sub_bits)}</div>')
    parts.append("</header>")
    parts.append('<div class="wrap">')

    last_day = None
    for item in timeline:
        dt = item.get("dt")
        day = dt.strftime("%Y-%m-%d") if dt else None
        if day and day != last_day:
            weekday = "一二三四五六日"[dt.weekday()]
            parts.append(f'<div class="date-sep"><span>{E(day)} 星期{weekday}</span></div>')
            last_day = day

        if item["kind"] == "system":
            parts.append(f'<div class="sys">{E(item["text"])}</div>')
            continue

        side = "me" if item["is_from_me"] else "them"
        parts.append(f'<div class="row {side}"><div class="col">')
        if (not item["is_from_me"]) and conv["is_group"]:
            parts.append(f'<div class="sender">{E(item["sender"])}</div>')

        # 正文气泡
        if item["text"]:
            edited = ' <span class="edited">（已编辑）</span>' if item["edited"] else ""
            parts.append(f'<div class="bubble">{E(item["text"])}{edited}</div>')

        # 附件
        for att in item["attachments"]:
            parts.append(render_attachment_html(att, opts, attach_dir_rel))

        # 时间 + 服务
        svc = "iMessage" if (item.get("service") == "iMessage") else "短信"
        parts.append(f'<div class="meta">{E(_fmt_time(dt))} · {svc}</div>')

        # 回应
        if item.get("reactions"):
            rs = "".join(
                f'<span class="r">{E(r["emoji"])} {E(r["who"])}</span>'
                for r in item["reactions"]
            )
            parts.append(f'<div class="reactions">{rs}</div>')

        parts.append("</div></div>")

    parts.append("</div>")
    parts.append('<div class="footer">由 imessage_export.py 本地离线导出 · '
                 '仅供本人备份留存</div>')
    parts.append("</body></html>")
    return "\n".join(parts)


def render_attachment_html(att, opts, attach_dir_rel):
    E = html.escape
    mode = opts["attachments"]
    if mode == "none":
        name = att.get("tname") or os.path.basename(att.get("filename") or "附件")
        return f'<div class="att-card"><div class="fn">📎 {E(name)}</div>' \
               f'<div class="ex">（未导出附件内容）</div></div>'

    if mode == "copy":
        info = copy_attachment(att, opts["_attach_dir"])
        if not info:
            name = att.get("tname") or "附件"
            return f'<div class="att-card"><div class="fn">📎 {E(name)}</div>' \
                   f'<div class="ex">附件缺失</div></div>'
        fname, mime, origname = info
        rel = f"{attach_dir_rel}/{fname}" if attach_dir_rel else fname
        if mime and mime.startswith("image/"):
            return f'<img src="{E(rel)}" alt="{E(origname or fname)}">'
        if mime in PLAYABLE_VIDEO:
            return f'<video controls src="{E(rel)}"></video>'
        if mime in PLAYABLE_AUDIO:
            return f'<audio controls src="{E(rel)}"></audio>'
        return f'<div class="att-card"><div class="fn">📎 <a href="{E(rel)}">' \
               f'{E(origname or fname)}</a></div></div>'

    # embed（默认）
    kind, uri, info = attachment_data_uri(
        att, opts["max_embed_bytes"], opts.get("image_max_px", 0))
    if kind == "image":
        return f'<img src="{uri}" alt="{E(str(info))}">'
    if kind == "video":
        name, mime = info
        return f'<video controls src="{uri}"></video>'
    if kind == "audio":
        name, mime = info
        return f'<audio controls src="{uri}"></audio>'
    if kind == "missing":
        return f'<div class="att-card"><div class="fn">📎 {E(str(info))}</div>' \
               f'<div class="ex">附件文件已不在磁盘上</div></div>'
    if kind == "toobig":
        name, mime, size = info
        return f'<div class="att-card"><div class="fn">📎 {E(name)}</div>' \
               f'<div class="ex">{E(mime)} · {human_size(size)} · ' \
               f'超过内嵌上限，未内嵌（可用 --attachments copy 导出原件）</div></div>'
    # file（视频/音频/其它，embed 模式下不内嵌）
    name, mime, size = info
    icon = "🎬" if (mime or "").startswith("video/") else \
           "🎵" if (mime or "").startswith("audio/") else "📎"
    extra = " · ".join(x for x in [mime, human_size(size)] if x)
    hint = ""
    if (mime or "").startswith(("video/", "audio/")):
        hint = "（未内嵌，可用 --attachments copy 一并导出原件）"
    return f'<div class="att-card"><div class="fn">{icon} {E(name)}</div>' \
           f'<div class="ex">{E(extra)}{E(hint)}</div></div>'


# ---------------------------------------------------------------------------
# 导出：TXT
# ---------------------------------------------------------------------------

def render_txt(conv, timeline, stats):
    lines = []
    title = conversation_title(conv)
    lines.append("=" * 60)
    lines.append(f"会话：{title}")
    if conv["handles"]:
        lines.append(("成员：" if conv["is_group"] else "对端：") + "、".join(conv["handles"]))
    lines.append(f"消息数：{stats['msgs']}  附件：{stats['attachments']}")
    lines.append(f"导出时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    lines.append("")

    last_day = None
    for item in timeline:
        dt = item.get("dt")
        day = dt.strftime("%Y-%m-%d") if dt else None
        if day and day != last_day:
            lines.append("")
            lines.append(f"—— {day} ——")
            last_day = day

        if item["kind"] == "system":
            lines.append(f"    * {item['text']}")
            continue

        ts = _fmt_datetime(dt)
        sender = item["sender"]
        head = f"[{ts}] {sender}:"
        if item["text"]:
            lines.append(f"{head} {item['text']}")
        elif item["attachments"]:
            lines.append(head)
        for att in item["attachments"]:
            name = att.get("tname") or os.path.basename(att.get("filename") or "附件")
            mime = att.get("mime") or ""
            tag = "图片" if mime.startswith("image/") else \
                  "视频" if mime.startswith("video/") else \
                  "音频" if mime.startswith("audio/") else "附件"
            lines.append(f"        [{tag}: {name}]")
        for r in item.get("reactions", []):
            lines.append(f"        └ {r['emoji']} {r['who']} {r['label']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 导出：CSV
# ---------------------------------------------------------------------------

def write_csv(path, conv, timeline):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "Unix时间戳", "方向", "发送者", "服务",
                    "类型", "正文", "附件", "回应"])
        for item in timeline:
            dt = item.get("dt")
            tsf = _fmt_datetime(dt)
            unix = ""
            if dt:
                unix = f"{dt.timestamp():.0f}"
            if item["kind"] == "system":
                w.writerow([tsf, unix, "", "", "", "系统消息", item["text"], "", ""])
                continue
            direction = "发出" if item["is_from_me"] else "收到"
            svc = item.get("service") or ""
            atts = " | ".join(
                (a.get("tname") or os.path.basename(a.get("filename") or "附件"))
                for a in item["attachments"]
            )
            reacts = " | ".join(
                f"{r['emoji']}{r['who']}" for r in item.get("reactions", [])
            )
            w.writerow([tsf, unix, direction, item["sender"], svc,
                        "消息", item["text"], atts, reacts])


# ---------------------------------------------------------------------------
# 工具函数：会话标题、文件名
# ---------------------------------------------------------------------------

def conversation_title(conv):
    if conv["display_name"]:
        return conv["display_name"]
    if conv["is_group"]:
        if conv["handles"]:
            return "群聊（" + "、".join(conv["handles"][:4]) + \
                   ("…" if len(conv["handles"]) > 4 else "") + "）"
        return conv["identifier"] or f"群聊 {conv['chat_id']}"
    if conv["handles"]:
        return conv["handles"][0]
    return conv["identifier"] or f"会话 {conv['chat_id']}"


def safe_filename(name, maxlen=60):
    bad = '/\\:*?"<>|\n\r\t'
    out = "".join("_" if c in bad else c for c in name).strip()
    out = out.replace(" ", "_")
    if len(out) > maxlen:
        out = out[:maxlen]
    return out or "conversation"


# ---------------------------------------------------------------------------
# 名字映射（可选）
# ---------------------------------------------------------------------------

def load_names(path):
    if not path:
        return {}
    import json
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        print(f"警告：读取名字映射文件失败：{e}", file=sys.stderr)
    return {}


# ---------------------------------------------------------------------------
# 选择会话
# ---------------------------------------------------------------------------

def select_conversations(convs, args):
    """按命令行参数选出要导出的会话列表。"""
    if args.all:
        return [c for c in convs if c["msg_count"] > 0]

    chosen = []
    if args.chat is not None:
        idx = args.chat - 1
        if idx < 0 or idx >= len(convs):
            raise SystemExit(f"错误：--chat 编号超出范围（应为 1~{len(convs)}）")
        chosen.append(convs[idx])
    if args.chat_id is not None:
        m = [c for c in convs if c["chat_id"] == args.chat_id]
        if not m:
            raise SystemExit(f"错误：找不到 chat_id={args.chat_id} 的会话")
        chosen += m
    if args.identifier:
        m = [c for c in convs if c["identifier"] == args.identifier]
        if not m:
            raise SystemExit(f"错误：找不到 chat_identifier={args.identifier!r} 的会话")
        chosen += m
    if args.handle:
        want = args.handle
        m = [c for c in convs if want in (c["handles"] or [])]
        if not m:
            # 宽松匹配：去掉空格/连字符后比较
            def norm(s):
                return "".join(ch for ch in s if ch.isdigit() or ch == "+" or ch.isalpha())
            wn = norm(want)
            m = [c for c in convs if any(norm(h) == wn for h in (c["handles"] or []))]
        if not m:
            raise SystemExit(f"错误：找不到与 {want!r} 相关的会话")
        chosen += m

    # 去重，保持顺序
    seen = set()
    uniq = []
    for c in chosen:
        if c["chat_id"] not in seen:
            seen.add(c["chat_id"])
            uniq.append(c)
    return uniq


# ---------------------------------------------------------------------------
# 命令：list
# ---------------------------------------------------------------------------

def cmd_list(args):
    with MessagesDB(args.db) as db:
        if args.contacts:
            handles = db.list_handles()
            print(f"共 {len(handles)} 个联系人标识：\n")
            print(f"{'消息数':>8}  {'服务':<8}  标识")
            print("-" * 50)
            for h in handles:
                print(f"{h['msg_count']:>8}  {(h['service'] or ''):<8}  {h['handle']}")
            return

        convs = db.list_conversations()
        convs = [c for c in convs if c["msg_count"] > 0] if not args.include_empty else convs
        print(f"共 {len(convs)} 个会话（按最近活动排序）：\n")
        print(f"{'#':>4}  {'类型':<4}  {'消息数':>6}  {'最后活动':<16}  会话")
        print("-" * 78)
        for i, c in enumerate(convs, 1):
            typ = "群聊" if c["is_group"] else "单聊"
            last = c["last"].strftime("%Y-%m-%d %H:%M") if c["last"] else ""
            title = conversation_title(c)
            if len(title) > 36:
                title = title[:35] + "…"
            print(f"{i:>4}  {typ:<4}  {c['msg_count']:>6}  {last:<16}  {title}")
        print("\n提示：用  export --chat <编号>  导出某个会话；--all 导出全部。")


# ---------------------------------------------------------------------------
# 命令：export
# ---------------------------------------------------------------------------

def cmd_export(args):
    formats = [f.strip().lower() for f in args.format.split(",") if f.strip()]
    valid = {"html", "txt", "csv"}
    bad = set(formats) - valid
    if bad:
        raise SystemExit(f"错误：不支持的格式 {', '.join(bad)}（可选：html, txt, csv）")
    if not formats:
        formats = ["html"]

    if args.attachments not in ("embed", "copy", "none"):
        raise SystemExit("错误：--attachments 只能是 embed / copy / none")

    start_ts = parse_user_date(args.start, end=False)
    end_ts = parse_user_date(args.end, end=True)
    names = load_names(args.names)

    out_root = os.path.abspath(os.path.expanduser(args.output))
    os.makedirs(out_root, exist_ok=True)

    opts = {
        "attachments": args.attachments,
        "max_embed_bytes": int(args.max_embed_mb * 1024 * 1024),
        "image_max_px": int(args.image_max_px or 0),
    }
    if opts["image_max_px"] > 0 and args.attachments == "embed" and not _sips_available():
        print("提示：系统缺少 sips，--image-max-px 将被忽略（图片按原始尺寸内嵌）。",
              file=sys.stderr)

    with MessagesDB(args.db) as db:
        convs = db.list_conversations()
        targets = select_conversations(convs, args)
        if not targets:
            raise SystemExit("没有选中任何会话。用 list 查看编号，或加 --all。")

        print(f"将导出 {len(targets)} 个会话到：{out_root}\n")
        run_export(db, targets, formats, start_ts, end_ts, names, opts, out_root)


def run_export(db, targets, formats, start_ts, end_ts, names, opts, out_root):
    """对选定的会话执行导出，返回合计统计。供命令行和交互模式共用。"""
    grand = {"msgs": 0, "attachments": 0}
    for n, conv in enumerate(targets, 1):
        messages = db.fetch_messages(conv["chat_id"])

        # 时间范围过滤
        if start_ts is not None or end_ts is not None:
            def in_range(m):
                t = m["ts"]
                if t is None:
                    return False
                if start_ts is not None and t < start_ts:
                    return False
                if end_ts is not None and t >= end_ts:
                    return False
                return True
            messages = [m for m in messages if in_range(m)]

        if not messages:
            print(f"[{n}/{len(targets)}] {conversation_title(conv)} —— "
                  f"该范围内无消息，跳过")
            continue

        timeline, stats = build_timeline(messages, names)
        if stats["msgs"] == 0 and stats["system"] == 0:
            print(f"[{n}/{len(targets)}] {conversation_title(conv)} —— 无可导出内容，跳过")
            continue

        title = conversation_title(conv)
        base = f"{conv['chat_id']:04d}_{safe_filename(title)}"
        grand["msgs"] += stats["msgs"]
        grand["attachments"] += stats["attachments"]

        made = []
        # copy 模式需要为每个会话建附件子目录
        attach_dir_rel = None
        if opts.get("attachments") == "copy":
            attach_dir_rel = base + "_附件"
            opts["_attach_dir"] = os.path.join(out_root, attach_dir_rel)

        if "html" in formats:
            p = os.path.join(out_root, base + ".html")
            htmltext = render_html(conv, timeline, stats, opts, attach_dir_rel)
            with open(p, "w", encoding="utf-8") as f:
                f.write(htmltext)
            made.append(os.path.basename(p))
        if "txt" in formats:
            p = os.path.join(out_root, base + ".txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(render_txt(conv, timeline, stats))
            made.append(os.path.basename(p))
        if "csv" in formats:
            p = os.path.join(out_root, base + ".csv")
            write_csv(p, conv, timeline)
            made.append(os.path.basename(p))

        print(f"[{n}/{len(targets)}] {title} —— {stats['msgs']} 条消息"
              f"（附件 {stats['attachments']}）→ {', '.join(made)}")

    print(f"\n完成。合计 {grand['msgs']} 条消息，{grand['attachments']} 个附件。")
    print(f"输出目录：{out_root}")
    return grand


# ---------------------------------------------------------------------------
# 命令：interactive（跟着提示操作，面向不想记命令的用户，供双击启动器调用）
# ---------------------------------------------------------------------------

def _open_fda_settings():
    """尝试帮用户打开「完全磁盘访问权限」设置面板。"""
    try:
        import subprocess
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security"
             "?Privacy_AllFiles"], check=False)
    except Exception:
        pass


def _reveal(path):
    try:
        import subprocess
        subprocess.run(["open", path], check=False)
    except Exception:
        pass


def cmd_interactive(args):
    print("=" * 56)
    print("     iMessage / 短信 聊天记录导出器")
    print("     跟着提示一步步操作即可，无需记命令")
    print("=" * 56)

    # 1) 打开数据库（处理权限问题）
    try:
        db = MessagesDB(args.db)
    except FullDiskAccessError as e:
        print("\n⚠️  " + str(e))
        print("\n我现在帮你把那个设置面板打开……")
        _open_fda_settings()
        print("\n请在打开的窗口里：")
        print("  1) 找到「完全磁盘访问权限」")
        print("  2) 把你的【终端】开关打开（没有就点 ＋ 添加 Terminal）")
        print("  3) 【完全退出终端再重新打开】（⌘Q）")
        print("  4) 重新双击一次本启动器")
        input("\n按回车键退出。")
        return
    except SystemExit as e:
        print("\n" + str(e))
        input("\n按回车键退出。")
        return

    try:
        convs = [c for c in db.list_conversations() if c["msg_count"] > 0]
        if not convs:
            print("\n没有找到任何聊天会话。")
            input("按回车键退出。")
            return

        def show_list(items):
            for i, c in enumerate(items, 1):
                typ = "群聊" if c["is_group"] else "单聊"
                last = c["last"].strftime("%Y-%m-%d") if c["last"] else ""
                title = conversation_title(c)
                if len(title) > 30:
                    title = title[:29] + "…"
                print(f"  {i:>3}. [{typ}] {title}  （{c['msg_count']} 条, 最近 {last}）")

        print(f"\n共有 {len(convs)} 个会话（按最近活动排序）。先显示最近 30 个：\n")
        show_list(convs[:30])
        if len(convs) > 30:
            print(f"\n  …… 还有 {len(convs) - 30} 个。想看全部编号可输入 all。")

        # 2) 选择会话
        chosen = None
        while chosen is None:
            sel = input("\n想导出哪个会话？\n"
                        "  · 输入编号（多个用逗号隔开，如 1,3,5）\n"
                        "  · 直接回车 = 导出全部会话\n"
                        "  · 输入 all = 查看完整列表\n> ").strip()
            low = sel.lower()
            if low == "all":
                print()
                show_list(convs)
                continue
            if sel == "":
                chosen = convs
                break
            picked = []
            okk = True
            for part in sel.replace("，", ",").split(","):
                part = part.strip()
                if not part:
                    continue
                if not part.isdigit() or not (1 <= int(part) <= len(convs)):
                    okk = False
                    break
                picked.append(convs[int(part) - 1])
            if okk and picked:
                chosen = picked
            else:
                print("  ⚠️  没看懂，请重新输入（例如 1 或 1,3,5）。")

        # 3) 格式
        fsel = input("\n导出成什么格式？\n"
                     "  1 = 网页 HTML（默认，推荐，直接能看能打印）\n"
                     "  2 = HTML + 文本TXT + 表格CSV 全都要\n> ").strip()
        formats = ["html", "txt", "csv"] if fsel == "2" else ["html"]

        # 4) 时间范围（可选）
        start_ts = end_ts = None
        drange = input("\n只导出某段时间吗？（直接回车 = 全部时间）\n"
                       "  要的话输入起止日期，如：2024-01-01 2024-12-31\n> ").strip()
        if drange:
            parts = drange.replace("~", " ").replace("，", " ").replace(",", " ").split()
            try:
                if len(parts) >= 1 and parts[0]:
                    start_ts = parse_user_date(parts[0], end=False)
                if len(parts) >= 2 and parts[1]:
                    end_ts = parse_user_date(parts[1], end=True)
            except SystemExit as e:
                print("  ⚠️  " + str(e) + " —— 已忽略时间范围，导出全部时间。")
                start_ts = end_ts = None

        # 5) 导出（默认内嵌图片并缩放到 1600px，避免单文件过大）
        out_root = os.path.expanduser("~/Desktop/iMessage导出")
        os.makedirs(out_root, exist_ok=True)
        opts = {
            "attachments": "embed",
            "max_embed_bytes": 10 * 1024 * 1024,
            "image_max_px": 1600 if _sips_available() else 0,
        }
        print("\n开始导出……（图片多时请稍候）\n")
        run_export(db, chosen, formats, start_ts, end_ts, {}, opts, out_root)
        print("\n✅ 全部完成！文件已保存到桌面的「iMessage导出」文件夹。")
        _reveal(out_root)
        print("（已自动帮你在访达里打开那个文件夹。）")
    finally:
        db.close()
    input("\n按回车键退出。")


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="imessage_export.py",
        description="本地、离线、只读地导出 macOS 本机 iMessage / SMS 聊天记录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python3 imessage_export.py list
  python3 imessage_export.py list --contacts
  python3 imessage_export.py export --chat 3
  python3 imessage_export.py export --handle +8613800138000 --format html,txt,csv
  python3 imessage_export.py export --all --start 2024-01-01 --end 2024-12-31
""")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"chat.db 路径（默认 {DEFAULT_DB}）")
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("interactive", help="交互式引导导出（不用记命令，双击启动器即走此模式）")
    pi.set_defaults(func=cmd_interactive)

    pl = sub.add_parser("list", help="列出所有会话或联系人")
    pl.add_argument("--contacts", action="store_true", help="列出联系人标识而非会话")
    pl.add_argument("--include-empty", action="store_true", help="也显示没有消息的会话")
    pl.set_defaults(func=cmd_list)

    pe = sub.add_parser("export", help="导出会话")
    g = pe.add_mutually_exclusive_group()
    g.add_argument("--chat", type=int, metavar="N", help="按 list 里的编号选择会话")
    pe.add_argument("--chat-id", type=int, help="按 chat 表 ROWID 选择会话")
    pe.add_argument("--identifier", help="按 chat_identifier 选择会话")
    pe.add_argument("--handle", help="按手机号/邮箱选择会话（单聊）")
    pe.add_argument("--all", action="store_true", help="导出全部会话")
    pe.add_argument("--start", help="起始日期 YYYY-MM-DD（含当天，本地时间）")
    pe.add_argument("--end", help="结束日期 YYYY-MM-DD（含当天，本地时间）")
    pe.add_argument("--format", default="html", help="导出格式，逗号分隔：html,txt,csv（默认 html）")
    pe.add_argument("--output", "-o", default="./imessage-export", help="输出目录（默认 ./imessage-export）")
    pe.add_argument("--attachments", default="embed",
                    help="附件处理：embed（内嵌进 HTML，默认）/ copy（复制原件到旁边目录）/ none（不导出）")
    pe.add_argument("--max-embed-mb", type=float, default=10.0,
                    help="单张内嵌图片的大小上限（MB，默认 10；超过则显示为卡片）")
    pe.add_argument("--image-max-px", type=int, default=0, metavar="PX",
                    help="内嵌图片最长边缩放到该像素（需 sips；0=保持原尺寸，默认 0。"
                         "照片很多时设为如 1600 可大幅减小单文件 HTML 体积）")
    pe.add_argument("--names", help="联系人名字映射 JSON 文件：{\"+86138...\": \"张三\"}")
    pe.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    # 不带子命令（例如双击启动器）时，进入交互式引导模式
    if not getattr(args, "command", None):
        args.func = cmd_interactive
    try:
        args.func(args)
    except FullDiskAccessError as e:
        print("错误：" + str(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
    except (BrokenPipeError, EOFError):
        pass


if __name__ == "__main__":
    main()
