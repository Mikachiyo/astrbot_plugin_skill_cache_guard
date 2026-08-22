"""astrbot_plugin_skill_cache_guard - 技能目录缓存保护（仿 deepseek-harness）

AstrBot 原生行为（astrbot/core/astr_main_agent.py:467）：每条用户消息都会
重新扫描技能目录，并把最新技能目录拼进 system prompt。对话中一旦新建/修改
技能，下一条请求的 system prompt 前缀随之变化，LLM 上下文缓存整体失效。

本插件复刻 dsh（deepseek-ai/deepseek-harness）的缓存友好策略：

1. 稳定前缀：on_llm_request 时把 AstrBot 拼进 system prompt 的技能目录段
   剥离，使 system prompt 不再随技能增删而变化（字节级稳定）。
2. 尾部注入：技能目录改为一条持久的 user 消息（<system-reminder> 包裹
   <available_skills>，首行带 digest 标记），注入到当前上下文位置（消息
   列表尾部）。检测到技能变化时只在尾部追加一条"全量替换"目录消息，旧目录
   消息不删不改（append-only），已有前缀缓存完全保留。
3. 压缩重整：猴子补丁拦截 ContextManager.process（压缩/截断的唯一入口，
   思路参考 astrbot_plugin_Mikachiyo_relationship，但检测改用零误报的
   恒等判断）。历史被改写时缓存必然失效，此时把最新目录合并去重并移动到
   system 消息之后（前缀区）；若目录在压缩中丢失，则重新注入最新目录。
4. 全会话生效：注入检测在每个会话各自的请求上进行。任何会话（不只是新建
   技能的那个会话）在其下次请求时都会以尾部追加方式获得最新目录，各自的
   system prompt 前缀都不被破坏。

注意：不要与 astrbot_plugin_skill_inject 同时启用（功能重叠，会互相干扰）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.message import Message

# ---------------------------------------------------------------------------
# 技能目录段定位与目录消息格式
# ---------------------------------------------------------------------------

# AstrBot build_skills_prompt 生成的段落（astrbot/core/skills/skill_manager.py:206）。
# 该段以 f"\n{section}\n" 拼进 system prompt（astr_main_agent.py:467），
# 结尾固定为第 7 条规则的 "...best alternative.\n"。
_SKILLS_SECTION_RE = re.compile(
    r"\n?## Skills\n\nYou have specialized skills.*?"
    r"### Available skills\n\n(?P<entries>.*?)\n\n### Skill rules\n\n.*?"
    r"7\. \*\*Failure handling\*\*.*?best alternative\.\n\n?",
    re.DOTALL,
)

# 目录消息首行标记（HTML 注释，WebUI/Markdown 渲染不可见），内嵌 digest。
_CATALOG_MARKER_RE = re.compile(r"^<!-- skill-catalog:([0-9a-f]{16}) -->")

_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()[:16]


def _digest_entries(entries_block: str) -> str:
    return hashlib.sha256(entries_block.encode("utf-8")).hexdigest()[:16]


def _catalog_digest_of_content(content: Any) -> str | None:
    """从消息内容中识别本插件注入的目录消息，返回 digest 或 None。"""
    if not isinstance(content, str):
        return None
    match = _CATALOG_MARKER_RE.match(content)
    return match.group(1) if match else None


def _catalog_digest_of_message(msg: Any) -> str | None:
    """识别 Message 对象是否为目录消息（限定 user 角色，避免模型复述误判）。"""
    if getattr(msg, "role", None) != "user":
        return None
    return _catalog_digest_of_content(getattr(msg, "content", None))


def _catalog_digest_of_context_item(item: Any) -> str | None:
    """识别 req.contexts 中的条目（dict 或 Message）是否为目录消息。"""
    if isinstance(item, dict):
        if item.get("role") != "user":
            return None
        return _catalog_digest_of_content(item.get("content"))
    return _catalog_digest_of_message(item)


def _build_catalog_content(entries_block: str, digest: str, kind: str) -> str:
    """渲染 dsh 风格的目录消息（全量快照，name+description+SKILL.md 路径）。"""
    entries = entries_block.strip("\n")
    if kind == "initial":
        intro = (
            "A skill is a reusable set of task-specific instructions stored in "
            "a SKILL.md file. The following skills are available in this session:"
        )
    else:
        intro = (
            "The available skill catalog has changed. This complete catalog "
            "replaces every earlier available-skills list in this session:"
        )
    if entries:
        listing = entries
        closing = (
            "If the user names a skill, or the task clearly matches a skill's "
            "description, you MUST first read that skill's SKILL.md with the "
            "absolute path shown above before applying it. Do not use skill "
            "names that appear only in earlier lists."
        )
    else:
        listing = "(none)"
        closing = (
            "No skills are currently available. Ignore every earlier "
            "available-skills list in this session."
        )
    return (
        f"<!-- skill-catalog:{digest} -->\n"
        "<system-reminder>\n"
        f"{intro}\n\n"
        "<available_skills>\n"
        f"{listing}\n"
        "</available_skills>\n\n"
        f"{closing}\n"
        "</system-reminder>"
    )


def _leading_system_count(messages: list[Any]) -> int:
    """消息列表开头连续 system 消息的数量（目录在前缀区的插入位置）。"""
    count = 0
    for msg in messages:
        if getattr(msg, "role", None) == "system":
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# ContextManager.process 猴子补丁：压缩/截断时重整目录
# ---------------------------------------------------------------------------
_original_process = None
_patch_installed = False


async def _patched_process(
    self_obj: ContextManager,
    messages: list[Message],
    trusted_token_usage: int = 0,
) -> list[Message]:
    """拦截 ContextManager.process，在历史被改写（压缩/截断）后重整目录。"""
    result = await _original_process(self_obj, messages, trusted_token_usage)

    plugin = SkillCacheGuardPlugin._instance
    if plugin is None or not plugin._enabled:
        return result
    # AstrBot 的截断/压缩在未做任何改动时返回原列表对象；
    # 返回新对象说明历史已被改写（此时缓存必然失效，重整目录零成本）。
    if result is messages:
        return result

    try:
        return plugin.maintain_catalog(messages, result)
    except Exception as exc:  # pragma: no cover - 重整失败不应阻断对话
        logger.error(f"[skill-cache-guard] 压缩后重整目录失败: {exc}", exc_info=True)
        return result


def _install_process_patch() -> None:
    global _original_process, _patch_installed
    if _patch_installed:
        return
    _original_process = ContextManager.process
    ContextManager.process = _patched_process
    _patch_installed = True
    logger.info("[skill-cache-guard] ContextManager.process 已打补丁")


def _uninstall_process_patch() -> None:
    global _patch_installed
    if not _patch_installed or _original_process is None:
        return
    ContextManager.process = _original_process
    _patch_installed = False


# ---------------------------------------------------------------------------
# 主插件类
# ---------------------------------------------------------------------------
class SkillCacheGuardPlugin(Star):
    _instance: "SkillCacheGuardPlugin | None" = None

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self._format_warned = False
        SkillCacheGuardPlugin._instance = self
        _install_process_patch()
        logger.info("[skill-cache-guard] 插件已加载（技能目录缓存保护）")

    @property
    def _enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    @property
    def _debug(self) -> bool:
        return bool(self.config.get("debug_log", False))

    # -----------------------------------------------------------------------
    # 请求钩子：剥离 system prompt 中的技能目录段，按需尾部追加最新目录
    # -----------------------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._enabled:
            return
        try:
            self._guard_request(req)
        except Exception as exc:  # pragma: no cover - 失败不应阻断对话
            logger.error(f"[skill-cache-guard] 请求处理失败: {exc}", exc_info=True)

    def _guard_request(self, req: ProviderRequest) -> None:
        sp = req.system_prompt or ""
        match = _SKILLS_SECTION_RE.search(sp)
        if match:
            entries_block = match.group("entries")
            # 剥离技能目录段：system prompt 从此不再随技能增删而变化
            req.system_prompt = _SKILLS_SECTION_RE.sub("", sp, count=1)
        else:
            entries_block = ""
            if "## Skills" in sp:
                # 技能段存在但格式已变化（AstrBot 版本更新？），放行原生行为
                if not self._format_warned:
                    self._format_warned = True
                    logger.warning(
                        "[skill-cache-guard] 检测到无法解析的技能目录段格式，"
                        "已放行原生注入（缓存保护对此请求不生效）"
                    )
                return

        digest = _digest_entries(entries_block)

        contexts = req.contexts
        if not isinstance(contexts, list):
            contexts = []
            req.contexts = contexts

        # 找该会话历史中最新一条目录消息（目录消息是持久的，会随历史保存）
        latest_digest = None
        for item in reversed(contexts):
            d = _catalog_digest_of_context_item(item)
            if d is not None:
                latest_digest = d
                break

        if latest_digest == digest:
            return  # 目录已最新：零改动，缓存完全保留
        if not match and latest_digest is None:
            return  # 无技能且从未注入过目录：无需处理

        kind = "initial" if latest_digest is None else "update"
        contexts.append(
            {
                "role": "user",
                "content": _build_catalog_content(entries_block, digest, kind),
            }
        )
        if self._debug:
            label = "初始目录" if kind == "initial" else "更新目录"
            logger.info(
                f"[skill-cache-guard] 已在上下文尾部追加{label} "
                f"(digest={digest}, skills={entries_block.count('- **')})"
            )

    # -----------------------------------------------------------------------
    # 压缩/截断后的目录重整（由 _patched_process 调用）
    # -----------------------------------------------------------------------
    def maintain_catalog(
        self, before: list[Message], after: list[Message]
    ) -> list[Message]:
        """历史被改写后：最新目录合并去重并移动到 system 消息之后；丢失则找回。"""
        catalogs = [m for m in after if _catalog_digest_of_message(m) is not None]
        if catalogs:
            latest = catalogs[-1]
            latest_idx = next(i for i, m in enumerate(after) if m is latest)
            if len(catalogs) == 1 and latest_idx == _leading_system_count(after):
                return after  # 已唯一且位于前缀区，无需改动
            # 缓存已失效，趁机合并去重：只保留最新目录并移到前缀区
            cleaned = [m for m in after if _catalog_digest_of_message(m) is None]
            cleaned.insert(_leading_system_count(cleaned), latest)
            if self._debug:
                logger.info(
                    f"[skill-cache-guard] 压缩/截断后重整目录："
                    f"合并 {len(catalogs)} 条 -> 1 条并移至 system 消息之后"
                )
            return cleaned

        # 目录在压缩中丢失（如 LLM 摘要吞掉）：从压缩前的消息里找回最新目录重新注入
        for msg in reversed(before):
            if _catalog_digest_of_message(msg) is not None:
                after.insert(_leading_system_count(after), msg)
                if self._debug:
                    logger.info("[skill-cache-guard] 目录在压缩中丢失，已重新注入")
                break
        return after

    async def terminate(self) -> None:
        _uninstall_process_patch()
        if SkillCacheGuardPlugin._instance is self:
            SkillCacheGuardPlugin._instance = None
        logger.info("[skill-cache-guard] 插件已卸载，ContextManager.process 已恢复")
