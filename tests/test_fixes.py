"""回归测试：v1.1.3 修复（不依赖 KiraAI core，sys.modules 打桩后用 SimpleNamespace 伪造 event）。

覆盖：
1. 系统事件/提醒（publish_notice 注入、system_ 前缀触发）不计骚扰信号
2. manage_ignore 解除 session 屏蔽生效（存储键 (sid, "*", kind)）
3. apply/unblock 的 "all" 展开一致（含 bot_speech/user_msgs/session_msgs）
4. _ignore_ctx 跨会话竞态：上下文 sid 与记录不一致时屏蔽落到正确会话
5. 真实 poke notice（is_notice + 真实用户 sender）仍正常计数（不被系统事件判定误杀）
"""

import asyncio
import logging
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---- KiraAI core 打桩（import main/harass_detect 前必须就位） ----

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _install_core_stubs():
    def _mk(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    _mk("core")
    core_plugin = _mk("core.plugin")
    core_chat = _mk("core.chat")  # 故意不提供 MessageChain → main 里 try import 回退 None
    core_chat_mu = _mk("core.chat.message_utils")
    core_chat_me = _mk("core.chat.message_elements")
    core_provider = _mk("core.provider")

    class BasePlugin:
        def __init__(self, ctx, cfg):
            self.ctx = ctx
            self.cfg = cfg

    def _deco_factory(*a, **k):
        return lambda f: f

    class _Dispatcher:
        def __getattr__(self, name):
            return _deco_factory

    class Priority:
        HIGH = 0
        LOW = 10

    class Text:
        def __init__(self, text=""):
            self.text = text

    class Reply:
        pass

    class KiraMessageEvent:
        pass

    class KiraMessageBatchEvent:
        pass

    class LLMRequest:
        pass

    core_plugin.BasePlugin = BasePlugin
    core_plugin.logger = logging.getLogger("anti_harass_test")
    core_plugin.on = _Dispatcher()
    core_plugin.Priority = Priority
    core_plugin.register = _Dispatcher()
    core_chat_mu.KiraMessageEvent = KiraMessageEvent
    core_chat_mu.KiraMessageBatchEvent = KiraMessageBatchEvent
    core_chat_me.Text = Text
    core_chat_me.Reply = Reply
    core_provider.LLMRequest = LLMRequest


_install_core_stubs()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import main as plugin_main  # noqa: E402
from harass_detect import ALL_KINDS, EXTRA_KINDS, HarassDetector  # noqa: E402

AntiHarassPlugin = plugin_main.AntiHarassPlugin
Text = sys.modules["core.chat.message_elements"].Text


def make_plugin() -> AntiHarassPlugin:
    ctx = SimpleNamespace(get_plugin_data_dir=lambda: tempfile.mkdtemp())
    return AntiHarassPlugin(ctx, {})


def make_event(sid="s1", uid="u1", nick="alice", is_notice=False, mid="m1",
               mentioned=True, text="hi", group=True, raw=None):
    msg = SimpleNamespace(
        sender=SimpleNamespace(user_id=uid, nickname=nick),
        chain=[Text(text)] if text is not None else [],
        is_mentioned=mentioned,
        message_id=mid,
    )
    return SimpleNamespace(
        session=SimpleNamespace(sid=sid),
        message=msg,
        message_id=mid,
        is_notice=is_notice,
        is_mentioned=mentioned,
        is_group_message=lambda: group,
        discard=lambda: None,
        raw_message=raw,
    )


# ---- 1. 系统事件/提醒不计骚扰 ----

def test_system_event_not_counted():
    plugin = make_plugin()

    # publish_notice 注入的提醒：is_notice=True, nickname="system", uid="unknown", is_mentioned=True
    ev_notice = make_event(uid="unknown", nick="system", is_notice=True, mid="system_message")
    asyncio.run(plugin.handle_msg(ev_notice))

    # 系统触发事件：uid 以 system_ 前缀开头（链非空、is_mentioned=True，此前会被计为 at）
    ev_sys = make_event(uid="system_proactive_dm", nick="system")
    asyncio.run(plugin.handle_msg(ev_sys))

    assert len(plugin.harass._counts) == 0, f"系统事件被计入了骚扰统计: {dict(plugin.harass._counts)}"
    assert len(plugin._extra_counts) == 0

    # 对照：真实用户 @ 消息仍正常计数
    asyncio.run(plugin.handle_msg(make_event(uid="u1")))
    assert len(plugin.harass._counts["s1"]["at"]) == 1


# ---- 2. manage_ignore 解除 session 屏蔽 ----

def test_session_unblock():
    plugin = make_plugin()
    ev = SimpleNamespace(session=SimpleNamespace(sid="s1"))

    asyncio.run(plugin.manage_ignore(ev, "block", "session", duration=100))
    assert plugin.harass.is_blocked("s1", "u1", time.time())

    result = asyncio.run(plugin.manage_ignore(ev, "unblock", "session", target_id="s1"))
    assert not plugin.harass.is_blocked("s1", "u1", time.time()), f"session 解除未生效: {result}"


# ---- 3. "all" 展开一致性（apply 与 unblock 同款全集） ----

def test_unblock_all_consistency():
    det = HarassDetector({}, None)
    det.apply_ignore("s1", "u1", "all", 100)
    now = time.time()
    for k in ALL_KINDS:
        assert det.is_ignored("s1", "u1", k, now), f"apply(all) 未覆盖 {k}"
    # 额外信号类确实在全集里
    for k in EXTRA_KINDS:
        assert k in ALL_KINDS

    det.unblock("s1", "u1", "all")
    for k in ALL_KINDS:
        assert not det.is_ignored("s1", "u1", k, now), f"unblock(all) 未解除 {k}"


# ---- 4. _ignore_ctx 跨会话竞态 ----

def test_ignore_ctx_race():
    plugin = make_plugin()
    # 会话 A 产生了 LLM 回复（写入 sid=A 的忽略上下文）
    asyncio.run(plugin.on_llm_response(SimpleNamespace(sid="A"), SimpleNamespace(tool_calls=None)))
    assert "A" in plugin._ignore_ctx

    # 模拟上下文错标为 B（B 没有任何回复记录），ignore tag 仍应作用到 A
    token = plugin_main._RESP_SID.set("B")
    try:
        asyncio.run(plugin.handle_ignore("user:u1|duration:60"))
    finally:
        plugin_main._RESP_SID.reset(token)

    now = time.time()
    assert plugin.harass.is_blocked("A", "u1", now), "屏蔽未落到记录中的会话 A"
    assert not plugin.harass.is_blocked("B", "u1", now), "屏蔽误落到上下文会话 B"


# ---- 5. 真实 poke notice 不被系统事件判定误杀 ----

def test_real_poke_notice_still_counted():
    plugin = make_plugin()
    ev = make_event(
        uid="u1", nick="alice", is_notice=True, mid="",
        text="[Poke] u1 戳了戳你",
        raw={"notice_type": "notify", "sub_type": "poke"},
    )
    asyncio.run(plugin.handle_msg(ev))
    assert len(plugin.harass._counts["s1"]["poke"]) == 1
