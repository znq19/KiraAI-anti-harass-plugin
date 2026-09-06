# KiraAI_anti_harass_plugin/防骚扰 v1.1.2

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/znq19/KiraAI_anti_harass_plugin)

# — 让 AI 学会“拒绝骚扰”

> 一个独立的骚扰屏蔽插件：检测戳一戳、连续 at、连续关键词、引用唤醒、刷屏等骚扰信号，通知 bot，由 bot 用 XML 自主决策屏蔽谁、屏蔽多久。屏蔽名单持久化，重启不丢。

---

## 🎯 定位

本插件是从 **KiraAI_sustained_chat_plugin（s 版）** 与 **KiraAI_Default-Chat-Z（z 版）** 新版本中**拆出的独立防骚扰插件**。

- **与 s/z 新版本互斥**：s/z 新版本内置了同等的骚扰感知能力并接管，因此**不要**同时安装本插件与 s/z 新版本，否则会冲突。
- **适用场景**：如果你用的是**旧版** s/z（不含骚扰感知），或想单独给其他聊天插件补上防骚扰能力，就装本插件。
- 若同时安装，s/z 新版本 `initialize` 时会检测到本插件已加载并提示停用、由聊天插件接管。

---

## 🚨 6 类信号检测（各自独立开关）

| 信号 | 检测方式 | 默认开关 |
|------|----------|----------|
| **戳一戳** | 时间窗内被戳次数达阈值 | ✅ 开 |
| **连续 at** | 时间窗内被 @ 次数达阈值 | ✅ 开 |
| **连续关键词** | 时间窗内命中唤醒词次数达阈值 | ⬜ 关 |
| **引用唤醒** | 时间窗内被引用回复次数达阈值 | ⬜ 关 |
| **bot 发言条数** | 时间窗内 bot 自己发言条数达阈值（防自己刷屏） | ⬜ 关 |
| **单用户消息条数** | 时间窗内某用户消息条数达阈值（防刷屏轰炸） | ⬜ 关 |
| **会话消息条数** | 时间窗内某会话消息条数达阈值（防群刷屏） | ⬜ 关 |

每个信号都有独立的**窗口时长**、**次数阈值**、**累计范围**（`per_user` 按单用户 / `all` 按会话）可配置。

检测到信号后，通过 **System 通知** 告知 bot，由 bot 决定是否屏蔽。

---

## 🧠 XML 决策

bot 收到通知后，用 XML tag 输出决策：

```xml
<ignore>user:X|type:Y|duration:N</ignore>   <!-- 屏蔽用户 X 的 Y 类骚扰 N 秒 -->
<ignore>all|type:Y|duration:N</ignore>     <!-- 屏蔽所有用户的 Y 类骚扰 -->
<ignore>none</ignore>                        <!-- 不屏蔽 -->
```

- `type` 取值：`poke` / `at` / `keyword` / `reply` / `bot_speech` / `user_msgs` / `session_msgs` / `all`
- `duration` 留空用默认值（180s）；`-1` 表示永久

---

## 🛠️ manage_ignore 工具

bot 可主动调用 `manage_ignore` 工具管理屏蔽名单，无需等通知：

| 动作 | 说明 |
|------|------|
| **block** | 屏蔽某个用户 / 会话 / 全局，可指定唤醒方式与时长 |
| **unblock** | 提前解除某个用户 / 会话的屏蔽 |
| **list** | 查看当前屏蔽列表 |

```json
manage_ignore(action="block", target_type="user", target_id="123456", block_type="at", duration=300)
```

---

## ⚙️ 屏蔽参数

| 参数 | 默认 | 说明 |
|------|------|------|
| **默认屏蔽时长** | 180s | bot 不指定时长时使用的屏蔽时长 |
| **固定时长** | 0（不启用） | >0 时所有屏蔽都用该值，忽略 bot 传的时长 |
| **最大时长钳制** | 0（不限制） | >0 时 bot 传的时长超过则钳制 |
| **到期通知** | ✅ 开 | 屏蔽到期自动解除时通知 bot |
| **持久化** | ✅ 开 | 屏蔽名单保存到 data 目录，重启不丢 |

---

## 📦 安装方法

1. 将本插件文件夹放入 `data/plugins/`
2. 在 WebUI 插件设置中配置检测开关、阈值、屏蔽参数
3. 重启 KiraAI 或禁用/启用插件使配置生效
4. **注意**：若同时使用 s/z 新版本聊天插件，请停用本插件（聊天插件已内置同能力并接管）

---

## 📝 版本信息

- 当前版本：v1.1.2
- 兼容 KiraAI：v2.29.6+
- 作者：znq19

<details>
<summary>更新日志</summary>

### v1.1.2
- **-1 永久不再绕过钳制**：设置最大时长限制（max_duration/extra_max_duration>0）后，bot 输入 -1 按最大允许值执行（不再永久）；仅未启用上限时 -1 才真正永久；allow_bot_duration=False 时 -1 也强制默认时长。hint 已同步更新
- **白名单豁免**：`harass_whitelist_users` / `harass_whitelist_sessions` 中的用户/会话不受任何屏蔽影响（消息照常进入 LLM）——原先白名单仅挡检测不挡屏蔽

- **额外信号独立钳制配置**：user_msgs / bot_speech / session_msgs 不再兜落 poke 配置，新增 `extra_max_duration`（默认 300，0=不钳制）/ `extra_allow_bot_duration`（默认开）——bot 自设时长钳到上限，关闭则强制默认时长
- **通知动态教"允许最大值"**：额外信号通知里建议的 duration 动态取 `extra_max_duration`（未启钳制回落 `extra_default_duration`）；不再教 `-1`（永久仅在 hint 中说明，避免绕过钳制）
- **bot_speech 开关**：新增 `bot_speech_block_session`（默认开）——检测到 bot 发言过多时，通知教会话级拉黑标签 `<ignore>all|duration:N</ignore>`（输入 = 拉黑当前会话，所有消息停止进入 LLM，N 秒后自动恢复）；关闭则仅提醒（bot 自觉）
- **hint 补全**：`<ignore>` 标签描述补"-1 表示永久"；通知补"fully block"（全拉黑）选项
- 版本 v1.1.1 → v1.1.2

### v1.1.1

- **过滤空通知事件（QQ 戳一戳别人等系统通知）**：框架把所有 notice（poke 别人/运气王/头衔/荣誉/进退群/管理员等）以"message_id=None、零内容"的消息事件广播给插件，此前会被误判为 poke 等骚扰信号（计数/通知误报）并参与评分
- 修复后：`is_notice` 且消息链完全为空 → 丢弃，不进骚扰计数/通知；有内容的保留（poke bot 的 `[Poke …]` 文本仍正常触发 poke 信号，`[System: …]` 系统提示与真实消息不受影响）

### v1.1.0

- **修复私聊消息被误判为 at 骚扰（严重）**：框架 qq.py 私聊消息构造时 `is_mentioned=True` 写死（私聊=天然提及，无 @ 概念），而 `_detect_kind` 未区分群私——私聊每条消息都被统计成 at，60s 3 条即触发"User X at you"系统提示，bot 可能据此回复拉黑 tag。现 `_detect_kind` 对私聊只保留 poke 检测（at/关键词/引用对私聊无意义），私聊不再误判
- **私聊额外信号独立开关/参数（默认关）**：`dm_detect_user_msgs` / `dm_detect_session_msgs` + `dm_user_msgs_*` / `dm_session_msgs_*` 参数（section_detect / section_thresholds）；群聊 signal 统计不受影响；bot_speech 仅群聊
- **私聊 bot 可主动拉黑**：额外信号通知带 `<ignore>user:{uid}|type:{kind}|duration:N</ignore>`（定向屏蔽该信号）与全拉黑选项；`<ignore>` tag 支持内嵌用户 ID + `type:` 解析
- **验证**：私聊 3 条消息不再被判 at；私聊开关默认关、开启后达阈值触发；拉黑链（is_blocked）私聊用户生效（消息被 discard）；回归 6/6+e2e 通过

### v1.0.5

- **代码精简**：移除全量 chat_enhance.py 拷贝（890 行），改为独立轻量模块 harass_detect.py（259 行）——只保留本插件实际使用的 HarassDetector + 安全转换辅助，维护更容易、体积减半
- **修复 allow_bot_duration=False 语义**：bot 不允许自设屏蔽时长时强制用默认时长（之前仍采用建议值）

### v1.0.4

- 同步 chat_enhance.py：私聊独立 PresenceThrottle、score_gate 路由修复

### v1.0.1

- 拉黑语义：屏蔽=该用户/会话所有消息不再进入（含戳一戳/at/关键词/引用/刷屏）；type:poke 单独屏蔽只挡戳一戳
- 累计评分：用户消息 +1、bot 回复 -5，攒到阈值补触发一次后清零（必补）

**首个独立版本**

- 从 s/z 新版本拆出完整骚扰屏蔽能力
- 6 类信号检测（戳/at/关键词/引用/bot 发言/单用户消息/会话消息），各自独立开关
- XML 决策（`<ignore>user:X|type:Y|duration:N</ignore>` / `<ignore>none</ignore>`）
- `manage_ignore` 工具（block/unblock/list）
- 屏蔽参数：默认 180s、固定时长、最大时长钳制、到期通知、持久化
- 与 s/z 新版本互斥（聊天插件内置同能力并接管）

</details>

## 🙏 致谢

本插件的存在感节流（回少提高/回多降低）、休眠时段（起夜概率 + 维持期）等机制，在设计上参考并致敬了 **NoriEngine Chat**（[skyzhishui/kira-ai-plugin-noriengine-chat](https://github.com/skyzhishui/kira-ai-plugin-noriengine-chat)）的评分引擎思路——它率先用"存在感抑制 + 时段调度"让 KiraAI 在群聊中也有了心跳包的感受，监听全局消息成为可能，融合版在此基础上把语义判断交还给 LLM，规则只做节流与状态管理。感谢 skyzhishui 的先行探索。
