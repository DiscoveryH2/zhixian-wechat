# -*- coding: utf-8 -*-
"""消息区截图 → 谁说了什么。RapidOCR 吃 numpy，不落盘。"""
import difflib
from datetime import datetime
import re
import unicodedata

import numpy as np


_ENGINE = None


def _engine():
    """OCR 引擎全进程共用：一个实例 ~40MB，每个会话一个 Reader，不能各带一个。
    det_limit_type 默认 'min' 会把小图放大到短边 736，裁小反而更慢；必须 'max'。"""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR(intra_op_num_threads=4, det_limit_type="max", det_limit_side_len=4000)
    return _ENGINE


def read_title(header):
    """面板头部那一条截图 → 会话名（numpy RGB）。取最靠上的一行，同一行里取最左的
    （右边是图标按钮，OCR 不出字；下面那行是公告）。群聊的成员数「(422)」去掉，只留名字当 key。
    认不出返回 ""。一次约 60ms，所以调用方只在头部像素变了时才问。"""
    if header is None or not header.size:
        return ""
    res, _ = _engine()(header, use_cls=False)
    if not res:
        return ""
    first = min(res, key=lambda r: r[0][0][1])
    row = first[0][0][1] + (first[0][2][1] - first[0][0][1])  # 框底：顶在这之上的算同一行
    item = min((r for r in res if r[0][0][1] < row), key=lambda r: r[0][0][0])
    if float(item[2]) < 0.80:
        return ""
    # Keep group count/title exactly as seen; never fuzzy-merge conversations.
    return item[1].strip()


def who_said(chat, box):
    """按 OCR 框里的颜色分类，不看 x 坐标。返回 (谁, 底色, 墨高)：
    先看底色平不平：框里众数颜色占比 <45% 就是图片（头像/照片/表情包）里的字 → None 丢掉。
    绿底 → me；非绿且文字对底色对比度 ≥150 → her；其余（引用块、群里的发言人名、时间戳、系统提示、
    链接卡片描述——都是灰字，对比度 80~95）→ "gray"。
    实测：气泡正文对比度 178~208，me 绿泡 142~150，灰字 ≤ 93。深浅主题都靠这套。
    墨高 = 框里最长一段连续有字的行数（OCR 框对小字有固定 padding、还会蹭到上下行，不能拿框高比大小）。"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    reg = chat[int(min(ys)):int(max(ys)), int(min(xs)):int(max(xs))].astype(int)
    if reg.size == 0:
        return None, None, 0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    if cnt.max() / reg.shape[0] / reg.shape[1] < 0.45:
        # 文字必须落在平底色上：WGC 帧是精确像素，气泡/面板里众数颜色占 0.56~0.82，
        # 头像/照片/表情包里只有 0.1~0.3——那是图片里的字（头像上的「借仲夏夜之梦」之类），不是消息。
        # ponytail: 只对精确像素的帧成立；缩放/压缩过的截图（比如拿预览窗再截一次的图）底色会糊成几百种颜色，全会被当图片。
        return None, bg, 0
    diff = np.abs(reg @ [0.299, 0.587, 0.114] - bg @ [0.299, 0.587, 0.114])
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:
        return "me", bg, ink_h
    return ("her" if diff.max() >= 150 else "gray"), bg, ink_h


def similar(a, b):
    """同一段像素挪个位置 OCR 会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），按相似度判同一条。"""
    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字


def sidebar_tail_matches(full, area, lines, now=None, min_body_chars=4):
    """Fail closed unless the selected chat preview matches the visible tail.

    This is only a send/continuation gate. It never turns an ambiguous OCR
    batch into a live event on its own. Unsupported themes or layouts return
    False rather than guessing where the conversation ends.
    """
    if full is None or area is None or not lines or lines[-1][0] not in ('me', 'her', 'other'):
        return False
    x0, y0, x1, y1 = area[:4]
    if x0 < 145 or y1 - y0 < 100:
        return False
    xstart = max(0, int(x0 * .18))
    strip = full[:, xstart:x0].astype(np.int16)
    if strip.size == 0:
        return False
    green = ((strip[:, :, 1] > strip[:, :, 0] + 28) &
             (strip[:, :, 1] > strip[:, :, 2] + 12) & (strip[:, :, 1] > 70))
    active = green.mean(axis=1) > .55
    bands, start = [], None
    for y, chosen in enumerate(active):
        if chosen and start is None:
            start = y
        elif not chosen and start is not None:
            if 25 <= y - start <= 150:
                bands.append((start, y))
            start = None
    if start is not None and 25 <= len(active) - start <= 150:
        bands.append((start, len(active)))
    if len(bands) != 1:
        return False
    lo, hi = bands[0]
    rows, _ = _engine()(full[lo:hi, xstart:x0], use_cls=False)
    rows = sorted((row for row in (rows or []) if float(row[2]) >= .8),
                  key=lambda row: row[0][0][1])
    if len(rows) < 2:
        return False
    headline = ''.join(str(row[1]) for row in rows[:-1])
    preview = str(rows[-1][1])
    match = re.search(r'(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)', headline)
    if not match:
        return False
    stamp_minute = int(match[1]) * 60 + int(match[2])
    current = now or datetime.now()
    current_minute = current.hour * 60 + current.minute
    if int(match[1]) > 23 or not 0 <= current_minute - stamp_minute <= 5:
        return False
    compact = lambda value: re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))).casefold()
    body = compact(lines[-1][2])
    visible_preview = compact(preview)
    if len(body) < min_body_chars or body not in visible_preview:
        return False
    sender = compact(lines[-1][1] or '')
    if sender and re.search(r'[:：]', preview):
        named = compact(re.sub(r'^\[\d+条\]', '', re.split(r'[:：]', preview, maxsplit=1)[0]))
        if sender != named:
            return False
    # A recent time label in the chat pane further reduces the chance that a
    # repeated old bubble happens to have the same text as the latest preview.
    time_rows, _ = _engine()(full[y0:y1, x0:x1], use_cls=False)
    before_tail = []
    for row in time_rows or []:
        label = str(row[1])
        found = re.fullmatch(r'(\d{1,2}):([0-5]\d)', label)
        if found and float(row[0][0][1]) <= float(lines[-1][3]):
            before_tail.append((float(row[0][0][1]), int(found[1]) * 60 + int(found[2])))
    if not before_tail:
        return False
    latest_label = max(before_tail)[1]
    return 0 <= stamp_minute - latest_label <= 15


class Reader:
    """一个会话一个 Reader：lh/seen 各自算各自的，切走再切回来不会把旧消息当新的重报一遍。"""

    def __init__(self):
        self.ocr = _engine()
        self.lh = None  # 正常气泡字高，头一帧定
        self.seen = []  # [(who, name, text)]，累计，封顶 500

    def read(self, chat, pane_bg):
        """→ [(who, name, text, y)]，同一气泡的多行已合并。who ∈ me/her；name 群聊里是发言人，单聊 None。"""
        res, _ = self.ocr(chat, use_cls=False)
        W = chat.shape[1]
        # 群聊：每条 her 气泡上方一行灰色发言人名（靠左、短、不带冒号、印在面板底色上），从上往下扫，名字带给后面的气泡。
        # 引用块/时间戳/公告带冒号，链接卡片灰字印在气泡底色上，都不会被当成名字。
        # ponytail: 名字行被 OCR 漏掉时会挂到上一个人头上。
        name, raw = None, []
        for box, text, _ in sorted(res or [], key=lambda r: r[0][0][1]):
            kind, bg, h = who_said(chat, box)
            if kind == "gray":
                on_pane = np.abs(bg - pane_bg).sum() <= 6
                if on_pane and box[0][0] < 0.25 * W and len(text) <= 16 and not re.search("[:：]", text):
                    name = text
                continue
            if kind is None or (self.lh and h < 0.6 * self.lh):
                continue  # 字比正常气泡小得多 = 图片消息（截图/表情包）里的字，不是气泡
            raw.append((kind, name if kind == "her" else None, text, box[0][1], box[2][1], h))
        if not self.lh and len(raw) >= 3:
            self.lh = float(np.median([r[5] for r in raw]))
        # 同一气泡的多行合并：同人、上一行底到这一行顶的间距不到半个字高（不同气泡之间至少隔一个字高）
        lines = []
        for who, nm, text, top, bottom, h in raw:
            if lines and lines[-1][0] == who and lines[-1][1] == nm and top - lines[-1][4] < 0.6 * (self.lh or h):
                lines[-1][2] += text
                lines[-1][4] = bottom
            else:
                lines.append([who, nm, text, top, bottom])
        return [(w, n, t, y) for w, n, t, y, _ in lines]

    def new_lines(self, lines):
        """去重（滚动不重复）→ 这一帧里真正新出现的 [(who, name, text)]。
        本帧有已知行时只要已知行下方的：往上滚翻出来的旧消息在已知行上方，不算。
        本帧一行已知的都没有（大图把旧文字全顶出去了、切了聊天、滚远了）：全算，宁可多算不能漏。
        ponytail: 同一人连发两句一模一样的会吞一句——对触发分析无害。"""
        known_y = [y for w, n, t, y in lines if self._seen(w, n, t)]
        floor = max(known_y) if known_y else -1
        new = [(w, n, t) for w, n, t, y in lines if y > floor and not self._seen(w, n, t)]
        self.seen.extend((w, n, t) for w, n, t, _ in lines if not self._seen(w, n, t))
        del self.seen[:-500]
        return new

    def _seen(self, who, name, text):
        # 名字不参与判重：名字行滚出画面后同一条消息会从 her(LO) 变成 her，不能算新消息
        return any(w == who and similar(t, text) for w, _, t in self.seen)
