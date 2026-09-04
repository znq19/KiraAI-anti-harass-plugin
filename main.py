"""KiraAI 防骚扰插件（独立版）

从 s/z 新版本聊天插件拆出的完整骚扰屏蔽能力：
- 6 类信号检测：戳一戳 / 连续 at / 连续关键词 / 引用唤醒 / bot 发言条数 / 单用户消息条数 / 会话消息条数
- XML 决策：bot 输出 <ignore>user:X|type:Y|duration:N</ignore> 或 <ignore>none</ignore>
- 工具接口：manage_ignore（block/unblock/list），bot 可主动调用
- 屏蔽名单持久化（data 目录 json，重启不丢）
- 固定屏蔽时长 / 最大屏蔽时长钳制

与 s/z 新版本互斥：s/z 新版本 initialize 时检测本插件已加载则提示停用并接管。
"""

import asyncio
import json
import os
import sys
import time
from collections import defaultdict, deque
from typing import Optional

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import importlib
for _m in ("chat_enhance",):
    if _m in sys.modules:
        try:
            importlib.reload(sys.modules[_m])
        except Exception:
            pass

from core.plugin import BasePlugin, logger, on, Priority, register
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.provider import LLMRequest
from core.chat.message_elements import Text, Reply
try:
    from core.chat import MessageChain
except Exception:
    MessageChain = None
from chat_enhance import HarassDetector, _safe_int, _safe_float

# 额外信号（bot 发言/单用户消息/会话消息）的检测键
EXTRA_KINDS = ("bot_speech", "user_msgs", "session_msgs")


class AntiHarassPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.data_dir = None
        self._load_config(cfg)
        # 核心检测器（复用 chat_enhance 的 HarassDetector，用 section 结构配置）
        self.harass = HarassDetector(self._harass_cfg, self)
        # 额外信号计数：sid -> kind -> deque[(ts, user_id)]
        self._extra_counts: dict[str, dict[str, deque]] = defaultdict(
            lambda: {k: deque(maxlen=512) for k in EXTRA_KINDS}
        )
        # 屏蔽名单持久化
        self._persist_path = None
        self._persist_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None

    def _load_config(self, cfg: dict) -> None:
        # schema 是 section 结构（section_detect/section_thresholds/section_ignore）
        detect = cfg.get("section_detect", {}) or {}
        th = cfg.get("section_thresholds", {}) or {}
        ig = cfg.get("section_ignore", {}) or {}
        self.detect_bot_speech = bool(detect.get("detect_bot_speech", False))
        self.detect_user_msgs = bool(detect.get("detect_user_msgs", False))
        self.detect_session_msgs = bool(detect.get("detect_session_msgs", False))
        self.bot_speech_window = _safe_float(th.get("bot_speech_window_seconds"), 300)
        self.bot_speech_threshold = _safe_int(th.get("bot_speech_threshold"), 10)
        self.user_msgs_window = _safe_float(th.get("user_msgs_window_seconds"), 60)
        self.user_msgs_threshold = _safe_int(th.get("user_msgs_threshold"), 10)
        self.session_msgs_window = _safe_float(th.get("session_msgs_window_seconds"), 60)
        self.session_msgs_threshold = _safe_int(th.get("session_msgs_threshold"), 20)
        self.default_ignore_duration = _safe_int(ig.get("default_ignore_duration"), 180)
        self.fixed_duration = _safe_int(ig.get("fixed_duration"), 0)
        self.max_duration = _safe_int(ig.get("max_duration"), 0)
        self.notify_unblock = bool(ig.get("notify_unblock", True))
        self.persist = bool(ig.get("persist", True))
        # 作用域/白名单（section_harass_scope）
        hscope = cfg.get("section_harass_scope", {}) or {}
        self.harass_scope_sessions = hscope.get("harass_scope_sessions", [])
        self.harass_whitelist_users = hscope.get("harass_whitelist_users", [])
        self.harass_whitelist_sessions = hscope.get("harass_whitelist_sessions", [])
        # 构造 HarassDetector 需要的 section 结构（section_poke/at/keyword/reply）
        self._harass_cfg = {}
        for kind, key in (("poke", "poke"), ("at", "at"), ("keyword", "keyword"), ("reply", "reply")):
            self._harass_cfg[f"section_{kind}"] = {
                "enabled": bool(detect.get(f"detect_{key}", kind in ("poke", "at"))),
                "window_seconds": _safe_float(th.get(f"{key}_window_seconds"), 60),
                "threshold": _safe_int(th.get(f"{key}_threshold"), 3 if kind != "keyword" else 5),
                "default_duration": self.default_ignore_duration,
                "allow_bot_duration": True,
                "max_duration": self.max_duration,
                "scope": th.get(f"{key}_scope", "per_user"),
            }
        self._harass_cfg["harass_scope_sessions"] = self.harass_scope_sessions
        self._harass_cfg["harass_whitelist_users"] = self.harass_whitelist_users
        self._harass_cfg["harass_whitelist_sessions"] = self.harass_whitelist_sessions

    async def initialize(self):
        self.data_dir = self.ctx.get_plugin_data_dir()
        if self.persist:
            self._persist_path = os.path.join(self.data_dir, "ignore_list.json")
            self._load_persist()
            self._persist_task = asyncio.create_task(self._persist_loop())
        # 额外信号计数回收（7 天闲置清理）
        self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info("[AntiHarass] 防骚扰插件已加载")

    async def terminate(self):
        if self._persist_task and not self._persist_task.done():
            self._persist_task.cancel()
        if self._prune_task and not self._prune_task.done():
            self._prune_task.cancel()
        self._save_persist()
        logger.info("[AntiHarass] 防骚扰插件已终止")

    async def _prune_loop(self):
        """回收 7 天无活动的额外信号计数（防历史会话无限增长）。"""
        while True:
            try:
                await asyncio.sleep(3600)
                now = time.time()
                for sid in list(self._extra_counts.keys()):
                    counts = self._extra_counts[sid]
                    # 所有 kind 的最近时间戳都超过 7 天 → 回收
                    recent = any(
                        any(ts >= now - 7 * 24 * 3600 for ts, _ in q)
                        for q in counts.values()
                    )
                    if not recent:
                        self._extra_counts.pop(sid, None)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[AntiHarass] prune loop error")

    # ---- 持久化 ----

    def _load_persist(self):
        try:
            if self._persist_path and os.path.exists(self._persist_path):
                with open(self._persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                for key, until in data.items():
                    if until > now:
                        parts = key.split("|")
                        if len(parts) == 3:
                            self.harass._ignored[(parts[0], parts[1], parts[2])] = until
                logger.info(f"[AntiHarass] 已加载 {len(self.harass._ignored)} 条持久化屏蔽")
        except Exception as e:
            logger.warning(f"[AntiHarass] 加载持久化失败: {e}")

    def _save_persist(self):
        try:
            if not self._persist_path:
                return
            now = time.time()
            data = {}
            for (sid, uid, kind), until in self.harass._ignored.items():
                if until > now:
                    data[f"{sid}|{uid}|{kind}"] = until
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[AntiHarass] 保存持久化失败: {e}")

    async def _persist_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                self._save_persist()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[AntiHarass] persist loop error")

    # ---- 消息入口 ----

    @on.im_message(priority=Priority.HIGH)
    async def handle_msg(self, event: KiraMessageEvent):
        sid = event.session.sid
        # 注意：不在此记录 _last_ignore_sid（旧实现）。ignore tag 是 LLM 回复的
        # 输出，on_llm_response 已记录本次回复所属会话；handle_msg 里记录会被
        # 任意新消息（含不触发 LLM 的围观消息）覆盖，造成 tag 作用到错误会话的竞态。
        now = time.time()
        user_id = str(event.message.sender.user_id) if event.message.sender else "unknown"

        # 核心检测（戳/at/关键词/引用）
        kind = self._detect_kind(event)
        if kind:
            notice = self.harass.check(sid, kind, user_id, now)
            if notice:
                await self._send_notice(sid, notice)

        # 额外信号
        if self.detect_user_msgs and not self.harass.is_ignored(sid, user_id, "user_msgs", now):
            self._extra_counts[sid]["user_msgs"].append((now, user_id))
            n = sum(1 for ts, u in self._extra_counts[sid]["user_msgs"] if ts >= now - self.user_msgs_window and u == user_id)
            if n >= self.user_msgs_threshold:
                self._extra_counts[sid]["user_msgs"].clear()
                await self._send_notice(sid, self._build_extra_notice("user_msgs", user_id, n, self.user_msgs_window, self.user_msgs_threshold))
        if self.detect_session_msgs and not self.harass.is_ignored(sid, user_id, "session_msgs", now):
            self._extra_counts[sid]["session_msgs"].append((now, user_id))
            n = sum(1 for ts, _ in self._extra_counts[sid]["session_msgs"] if ts >= now - self.session_msgs_window)
            if n >= self.session_msgs_threshold:
                self._extra_counts[sid]["session_msgs"].clear()
                await self._send_notice(sid, self._build_extra_notice("session_msgs", user_id, n, self.session_msgs_window, self.session_msgs_threshold))

    @on.llm_response(priority=Priority.HIGH)
    async def on_llm_response(self, event: KiraMessageBatchEvent, resp, *_):
        # 记录本次 LLM 回复所属会话（ignore tag 处理器用）。
        # 必须在最终文本回复时写：框架 tag 处理器无 event 上下文，_last_ignore_sid
        # 是唯一通道。写入已把竞态窗口缩到最小（on_llm_response 返回后框架才解析
        # XML 执行 tag，多会话并发回复时可能被覆盖，已知限制）。
        if getattr(resp, "tool_calls", None):
            return
        try:
            sid = str(event.sid)
        except Exception:
            sid = None
        if sid:
            self._last_ignore_sid = sid

    @on.message_sent(priority=Priority.LOW)
    async def on_message_sent(self, event, *_, **__):
        if not self.detect_bot_speech:
            return
        try:
            sid = str(event.session.sid)
        except Exception:
            return
        now = time.time()
        if not self.harass.is_ignored(sid, "bot", "bot_speech", now):
            self._extra_counts[sid]["bot_speech"].append((now, "bot"))
            n = sum(1 for ts, _ in self._extra_counts[sid]["bot_speech"] if ts >= now - self.bot_speech_window)
            if n >= self.bot_speech_threshold:
                self._extra_counts[sid]["bot_speech"].clear()
                await self._send_notice(sid, self._build_extra_notice("bot_speech", "bot", n, self.bot_speech_window, self.bot_speech_threshold))

    def _detect_kind(self, event) -> Optional[str]:
        if getattr(event, "is_notice", False):
            raw = getattr(event, "raw_message", None)
            if isinstance(raw, dict) and raw.get("notice_type") == "notify" and raw.get("sub_type") == "poke":
                return "poke"
            return None
        for m in getattr(event.message, "chain", []):
            if isinstance(m, Reply):
                return "reply"
        if getattr(event.message, "is_mentioned", False) or getattr(event, "is_mentioned", False):
            return "at"
        return None

    def _build_extra_notice(self, kind: str, user_id: str, n: int, window: float, threshold: int) -> str:
        label = {"bot_speech": "you spoke", "user_msgs": f"user {user_id} sent",
                 "session_msgs": "this session received"}[kind]
        dur = self.default_ignore_duration
        return (
            f"[System: {label} {n} messages in {int(window)}s (threshold {threshold}). "
            f"Reply with <ignore>user:{user_id}|type:{kind}|duration:{dur}</ignore> to block, "
            f"or <ignore>none</ignore> to do nothing. Ignore lasts {dur}s by default.]"
        )

    async def _send_notice(self, sid: str, text: str):
        try:
            if MessageChain is None:
                logger.warning(f"[AntiHarass] 通知发送跳过（框架模块不可用）: {sid}")
                return
            chain = MessageChain([Text(text)])
            await self.ctx.publish_notice(session=sid, chain=chain, is_mentioned=True)
        except Exception as e:
            logger.warning(f"[AntiHarass] 通知发送失败: {e}")

    # ---- XML tag ----

    @register.tag(name="ignore", description="屏蔽骚扰。输出 <ignore>user:X|type:Y|duration:N</ignore> 屏蔽用户 X 的 Y 类骚扰，<ignore>all|type:Y|duration:N</ignore> 屏蔽所有用户，<ignore>none</ignore> 不屏蔽。type 为 poke/at/keyword/reply/bot_speech/user_msgs/session_msgs/all。")
    async def handle_ignore(self, value: str, **kwargs) -> list:
        value = (value or "").strip()
        if not value or value.lower() == "none":
            return []
        parts = [p.strip() for p in value.split("|")]
        target = parts[0].lower()
        kind = "all"
        duration = 0
        for p in parts[1:]:
            if p.startswith("type:"):
                kind = p.split(":", 1)[1].strip() or "all"
            elif p.startswith("duration:"):
                try:
                    duration = int(p.split(":", 1)[1])
                except (ValueError, IndexError):
                    duration = 0
        try:
            sid = self._last_ignore_sid
        except AttributeError:
            sid = None
        if sid is None:
            return []
        # 固定时长优先
        if self.fixed_duration > 0:
            duration = self.fixed_duration
        elif duration <= 0:
            duration = self.default_ignore_duration
        if self.max_duration > 0:
            duration = min(duration, self.max_duration)
        uid = "*" if target == "all" else (target[5:] if target.startswith("user:") else target)
        # kind=all 时展开全部 7 类（4 核心 + 3 额外信号）
        kinds = ("poke", "at", "keyword", "reply", "bot_speech", "user_msgs", "session_msgs") if kind == "all" else (kind,)
        for k in kinds:
            if uid == "*":
                self.harass.apply_ignore(sid, "*", k, duration)
            elif uid and uid != "user":
                self.harass.apply_ignore(sid, uid, k, duration)
            else:
                # user 无具体 ID：反查最近触发者
                last_uid = self.harass._last_trigger_user(sid, k)
                if last_uid:
                    self.harass.apply_ignore(sid, last_uid, k, duration)
                else:
                    logger.warning(f"[AntiHarass] 无法确定目标用户，未屏蔽: {sid} {k}")
        logger.info(f"[AntiHarass] 屏蔽: {uid} {kind} {duration}s")
        return []

    # ---- 工具接口 ----

    @register.tool(
        name="manage_ignore",
        description="管理骚扰屏蔽：屏蔽某个用户/会话/某种唤醒方式，或提前解除屏蔽。bot 觉得被骚扰、或人设要求时调用。",
        params={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["block", "unblock", "list"],
                           "description": "block=屏蔽，unblock=解除屏蔽，list=查看当前屏蔽列表"},
                "target_type": {"type": "string", "enum": ["user", "session", "all"],
                                "description": "屏蔽对象：user=某个用户，session=某个会话，all=全局"},
                "target_id": {"type": "string",
                              "description": "目标 ID：target_type=user 时是用户 ID，=session 时是会话 ID，=all 时留空"},
                "block_type": {"type": "string", "enum": ["poke", "at", "keyword", "reply", "all"],
                               "description": "屏蔽的唤醒方式，默认 all", "default": "all"},
                "duration": {"type": "integer",
                             "description": "屏蔽时长（秒）。留空用默认；-1 永久", "default": 0},
            },
            "required": ["action", "target_type"],
        },
    )
    async def manage_ignore(self, event, action: str, target_type: str, target_id: str = "",
                            block_type: str = "all", duration: int = 0) -> str:
        try:
            sid = str(event.session.sid)
        except Exception:
            sid = str(getattr(event, "sid", ""))
        if action == "list":
            return self.harass.list_ignored(sid)
        if action == "unblock":
            if target_type == "all":
                return "请指定要解除的用户或会话"
            return self.harass.unblock(sid, target_id, block_type)
        if self.fixed_duration > 0:
            duration = self.fixed_duration
        elif duration <= 0:
            duration = self.default_ignore_duration
        if self.max_duration > 0:
            duration = min(duration, self.max_duration)
        if target_type in ("all", "session"):
            return self.harass.apply_ignore(sid, "*", block_type, duration)
        return self.harass.apply_ignore(sid, target_id, block_type, duration)
