# 技能目录缓存保护（astrbot_plugin_skill_cache_guard）

仿 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（DSH）的技能目录注入策略，解决 AstrBot 在对话中新建技能后立刻重建 system prompt、导致 LLM 上下文缓存整体失效的问题。

## 问题背景

AstrBot 原生行为（`astrbot/core/astr_main_agent.py:467`）：**每条**用户消息都会重新扫描技能目录，并把最新技能目录拼进 `system_prompt`。因此对话中 agent 一旦新建/修改技能，下一条请求的 system prompt 前缀就会变化——前缀缓存从第一个 token 起全部失效，长会话的每次请求都变成全量计费。

## 本插件的策略（与 DSH 一致）

1. **稳定前缀**：`on_llm_request` 时把技能目录段从 `system_prompt` 中剥离，system prompt 从此不再随技能增删而变化（字节级稳定）。
2. **尾部注入**：技能目录改为一条**持久的** user 消息（`<system-reminder>` 包裹 `<available_skills>`，含名称/描述/SKILL.md 绝对路径，首行带 digest 标记）注入到**当前上下文位置**（消息列表尾部）。检测到技能变化时，只在尾部追加一条"全量替换"目录消息，旧消息不删不改（append-only），已有前缀缓存完全保留。
3. **压缩重整**：猴子补丁拦截 `ContextManager.process`（压缩/截断的唯一入口，检测方式为零误报的恒等判断，时机遇上参考 [astrbot_plugin_Mikachiyo_relationship](https://github.com/Mikachiyo/astrbot_plugin_Mikachiyo_relationship)）。历史被改写时缓存必然失效，此时把最新目录**合并去重并移动到 system 消息之后**（前缀区）；若目录在压缩中丢失（如被 LLM 摘要吞掉），自动找回最新目录重新注入。
4. **全会话生效**：注入检测在每个会话各自的请求上进行。任何一个会话（不只是新建技能的那个）在其下次请求时都会以尾部追加方式获得最新目录，各自的 system prompt 前缀都不被破坏。

效果：技能不变时请求字节级稳定（满缓存命中）；技能变化时旧缓存一个 token 都不浪费，只为新追加的目录消息付费；目录的"大搬家"只发生在缓存必然失效的压缩时刻。

## 安装与使用

- 将本目录放入 AstrBot 的插件目录（如 `data/plugins/`），在 WebUI 启用即可。
- 配置项：`enable`（总开关）、`debug_log`（输出每次注入/重整日志，用于确认生效）。
- **不要与 `astrbot_plugin_skill_inject` 同时启用**（功能重叠，会互相干扰）。
- 插件状态只存在于内存：AstrBot 重启后，各会话的首条请求会按当时技能状态重新建立基线（与原生行为一致），之后恢复缓存保护。

## 注意事项 / 已知边界

- 目录消息以 **user 角色**注入（与 DSH 相同），对 OpenAI / Anthropic / Gemini 等各 provider 都安全；它会随会话历史持久化，直到某次压缩将其合并清理。
- AstrBot 的日期时间、用户标识等本来就是注入到**当前用户消息**（`extra_user_content_parts`）而非 system prompt，与本插件无冲突。
- 如果有其他插件（如某些 LTM/记忆插件）每轮改写 system prompt，缓存同样会失效，那不在本插件保护范围内。
- 子代理（subagent）的请求不经过 `on_llm_request`，仍走原生注入（行为不变，只是没有缓存保护）。
- 仅在 AstrBot v4.25.1 源码上验证过；若 AstrBot 的技能段格式变化，插件会识别并放行原生行为（日志告警），不会产生错误注入。
- 卸载/停用插件后，已注入历史的目录消息作为普通文本消息残留，无害；`ContextManager.process` 补丁会在插件终止时自动恢复。

## 离线测试

仓库内 `test_skill_cache_guard.py` 不依赖 AstrBot 运行环境，直接对插件核心逻辑做场景测试（注入稳定性、尾部追加、压缩重整、目录找回、全量删除墓碑等）：

```bash
python test_skill_cache_guard.py
```
