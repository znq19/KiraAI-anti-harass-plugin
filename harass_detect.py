"""骚扰检测器（独立模块，anti-harass 插件专用）。

从 chat_enhance.py 抽出 HarassDetector + 安全转换辅助函数，消除全量引擎拷贝。
- 频率检测 + XML 决策 + 屏蔽（戳一戳/连续 at/连续关键词/引用唤醒）
- allow_bot_duration=False 时强制默认屏蔽时长（忽略 bot 自设值）
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Optional

from core.plugin import logger


def _safe_int(v, default: int) -> int:
    """安全转 int：None/非数字/越界回退默认值。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n


def _safe_float(v, default: float) -> float:
    """安全转 float：None/非数字回退默认值。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


class HarassDetector:
    """频率检测 + XML 决策 + 屏蔽。

    每类骚扰独立配置（窗口/阈值/默认屏蔽时长/允许 bot 自设/最大时长/累计范围/开关）。
    屏蔽键：(sid, user_id, kind) 或 (sid, '*', kind)（all 累计/全局）。
    """

    KINDS = ("poke", "at", "keyword", "reply")

    def __init__(self, cfg: dict, plugin=None):
        self._plugin = plugin
        self._load(cfg)
        # 作用域/白名单（全局，所有类型共用）：
        # scope_sessions 非空时仅对这些会话检测（空=全部）；白名单命中不检测
        self._scope_sessions = set(str(x) for x in (cfg.get("harass_scope_sessions") or []))
        self._whitelist_users = set(str(x) for x in (cfg.get("harass_whitelist_users") or []))
        self._whitelist_sessions = set(str(x) for x in (cfg.get("harass_whitelist_sessions") or []))
        # sid -> kind -> deque[(ts, user_id)]
        self._counts: dict[str, dict[str, deque]] = defaultdict(
            lambda: {k: deque(maxlen=256) for k in self.KINDS}
        )
        # (sid, user_id, kind) -> until_ts
        self._ignored: dict[tuple, float] = {}
        # (sid, kind) -> 最近触发者（达阈值时记录，供 user|duration:N 无 ID 时反查）
        self._last_trigger_user_map: dict[tuple, str] = {}
        self._prune_task: Optional[asyncio.Task] = None

    def _load(self, cfg: dict) -> None:
        self._conf = {}
        for kind in self.KINDS:
            sec = cfg.get(f"section_{kind}", {}) or {}
            self._conf[kind] = {
                "enabled": bool(sec.get("enabled", kind in ("poke", "at"))),
                "window": max(1.0, _safe_float(sec.get("window_seconds"), 60)),
                "threshold": max(1, _safe_int(sec.get("threshold"), 3)),
                "default_duration": max(1, _safe_int(sec.get("default_duration"), 180)),
                "allow_bot_duration": bool(sec.get("allow_bot_duration", True)),
                "max_duration": max(0, _safe_int(sec.get("max_duration"), 300)),
                "scope": sec.get("scope", "per_user"),
            }

    # ---- 检测 ----

    def check(self, sid: str, kind: str, user_id: str, now: float) -> Optional[str]:
        """记录一次事件；达阈值返回通知文本，未达返回 None。"""
        conf = self._conf.get(kind)
        if not conf or not conf["enabled"]:
            return None
        # 作用域/白名单：会话不在作用域内、或用户/会话在白名单 → 不检测
        if self._scope_sessions and sid not in self._scope_sessions:
            return None
        if user_id in self._whitelist_users or sid in self._whitelist_sessions:
            return None
        if self.is_ignored(sid, user_id, kind, now):
            return None
        counts = self._counts[sid][kind]
        counts.append((now, user_id))
        # 窗口内计数（scope=all 时按会话累计，per_user 时按用户累计）
        cutoff = now - conf["window"]
        if conf["scope"] == "all":
            n = sum(1 for ts, _ in counts if ts >= cutoff)
        else:
            n = sum(1 for ts, u in counts if ts >= cutoff and u == user_id)
        if n < conf["threshold"]:
            return None
        # 达阈值：记录最近触发者（供 user|duration:N 无 ID 时反查），清空窗口计数避免重复触发
        self._last_trigger_user_map[(sid, kind)] = user_id
        self._counts[sid][kind] = deque(maxlen=256)
        return self._build_notice(kind, user_id, n, conf)

    def _build_notice(self, kind: str, user_id: str, n: int, conf: dict) -> str:
        tag = {"poke": "poke_ignore", "at": "at_ignore",
               "keyword": "kw_ignore", "reply": "reply_ignore"}[kind]
        dur = conf["default_duration"]
        max_dur = conf["max_duration"] if conf["allow_bot_duration"] else dur
        dur_txt = f" (max {max_dur}s)" if conf["allow_bot_duration"] and max_dur > dur else ""
        return (
            f"[System: User {user_id} {kind} you {n} times in {int(conf['window'])}s. "
            f"Reply with <{tag}>user|duration:{dur}</{tag}> to ignore this user, "
            f"<{tag}>all|duration:{dur}</{tag}> to ignore everyone, "
            f"or <{tag}>none</{tag}> to do nothing. "
            f"Ignore lasts {dur}s by default{dur_txt}.]"
        )

    # ---- 屏蔽 ----

    def is_ignored(self, sid: str, user_id: str, kind: str, now: float) -> bool:
        # 全局屏蔽（所有会话所有用户）
        if self._ignored.get(("*", "*", kind), 0.0) > now:
            return True
        # 会话级屏蔽（该会话所有用户）
        if self._ignored.get((sid, "*", kind), 0.0) > now:
            return True
        # 用户级屏蔽
        if self._ignored.get((sid, user_id, kind), 0.0) > now:
            return True
        return False

    def is_blocked(self, sid: str, user_id: str, now: float) -> bool:
        """拉黑语义：该用户/会话是否有「非 poke」的未过期屏蔽（含全局/会话级）。

        与 is_ignored 的区别：is_ignored 按 kind 精确匹配（检测跳过用）；
        is_blocked 不看 kind——只要该用户/会话被屏蔽过（无论 poke/at/keyword/
        reply 还是额外信号），其消息就完全不进 LLM（宿主 handle_msg 入口调用）。
        """
        for (s, uid, kind), until in self._ignored.items():
            if until <= now:
                continue
            if kind == "poke":
                # poke 屏蔽只挡戳一戳（通知事件），不拉黑普通消息
                continue
            if s == "*" or s == sid:
                if uid == "*" or uid == user_id:
                    return True
        return False

    def apply_ignore(self, sid: str, user_id: str, kind: str, duration: int) -> str:
        """执行屏蔽。user_id='*' 表示该会话内所有用户；sid='*' 表示全局（所有会话）。
        kind='all' 时展开为全部 4 类（poke/at/keyword/reply）。返回结果文本。"""
        # kind='all' 时用任一具体 kind 的配置（默认时长/钳制）
        conf = self._conf.get(kind) or self._conf.get("poke", {})
        if duration < 0:
            # -1 = 永久屏蔽（工具描述约定）
            until = float("inf")
        else:
            # allow_bot_duration=False：bot 不允许自设时长，强制用默认时长
            # （配置语义：仅允许使用默认屏蔽时长，忽略 bot 建议值）
            if not conf.get("allow_bot_duration", True):
                duration = conf.get("default_duration", 180)
            elif duration <= 0:
                duration = conf.get("default_duration", 180)
            if conf.get("allow_bot_duration", True) and conf.get("max_duration", 0) > 0:
                duration = min(duration, conf["max_duration"])
            until = time.time() + duration
        if kind == "all":
            # 拉黑语义：all = 全部形式（含 poke）——该用户/会话消息完全不进 LLM
            kinds = self.KINDS
        else:
            kinds = (kind,)
        for k in kinds:
            self._ignored[(sid, user_id, k)] = until
        if sid == "*":
            scope_txt = "all sessions, all users"
        elif user_id == "*":
            scope_txt = f"session {sid}, all users"
        else:
            scope_txt = f"user {user_id} in {sid}"
        return f"已屏蔽 {scope_txt} 的 {kind} 唤醒 {duration} 秒"

    def apply_ignore_from_tag(self, sid: str, kind: str, value: str) -> str:
        """解析 XML tag 值：user|duration:N / all|duration:N / none。"""
        value = (value or "").strip()
        if not value or value.lower() == "none":
            return ""
        parts = [p.strip() for p in value.split("|")]
        target = parts[0].lower()
        duration = 0
        for p in parts[1:]:
            if p.startswith("duration:"):
                try:
                    duration = int(p.split(":", 1)[1])
                except (ValueError, IndexError):
                    duration = 0
        if target in ("user", "all"):
            uid = "*" if target == "all" else None
            # user 需要具体用户 ID：tag 里没带时用最近触发者
            if uid is None:
                uid = self._last_trigger_user(sid, kind)
                if uid is None:
                    return "（无法确定目标用户，未屏蔽）"
            return self.apply_ignore(sid, uid, kind, duration)
        return ""

    def _last_trigger_user(self, sid: str, kind: str) -> Optional[str]:
        # 优先用达阈值时记录的最近触发者（窗口已清空，deque 里可能没有）
        uid = self._last_trigger_user_map.get((sid, kind))
        if uid:
            return uid
        counts = self._counts.get(sid, {}).get(kind)
        if counts:
            for ts, u in reversed(list(counts)):
                return u
        return None

    def unblock(self, sid: str, user_id: str, kind: str) -> str:
        kinds = self.KINDS if kind == "all" else (kind,)
        removed = 0
        for k in kinds:
            key = (sid, user_id, k)
            if key in self._ignored:
                self._ignored.pop(key, None)
                removed += 1
        if removed:
            return f"已解除 {user_id} 的 {kind} 屏蔽"
        return f"未找到 {user_id} 的 {kind} 屏蔽"

    def list_ignored(self, sid: str) -> str:
        now = time.time()
        rows = []
        for (s, uid, kind), until in self._ignored.items():
            # 全局屏蔽（s="*"）对所有会话可见
            if (s == sid or s == "*") and until > now:
                scope = "全局" if s == "*" else (f"会话{s}" if uid == "*" else f"用户{uid}")
                if until == float("inf"):
                    remain = "永久"
                else:
                    remain = f"{int(until - now)}s"
                rows.append(f"{scope} {kind} 剩余 {remain}")
        return "当前屏蔽: " + ("; ".join(rows) if rows else "无")

    def prune(self) -> None:
        now = time.time()
        for key in [k for k, v in self._ignored.items() if v <= now]:
            self._ignored.pop(key, None)
        # 回收 7 天无活动的检测计数（_counts / _last_trigger_user_map），防长期运行内存增长
        for sid in list(self._counts.keys()):
            last = 0.0
            for dq in self._counts[sid].values():
                if dq:
                    last = max(last, dq[-1][0])
            if now - last > 7 * 24 * 3600:
                self._counts.pop(sid, None)
                for key in [k for k in self._last_trigger_user_map if k[0] == sid]:
                    self._last_trigger_user_map.pop(key, None)
