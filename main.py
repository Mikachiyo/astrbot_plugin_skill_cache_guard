"""astrbot_plugin_skill_cache_guard - 技能目录缓存保护（仿 deepseek-harness）

AstrBot 原生行为（astrbot/core/astr_main_agent.py:467）：每条用户消息都会
重新扫描技能目录，并把最新技能目录拼进 system prompt。对话中一旦新建/修改
技能，下一条请求的 system prompt 前缀随之变化，LLM 上下文缓存整体失效。

本插件复刻 dsh（deepseek-ai/deepseek-harness）的缓存友好策略：

1. 稳定前缀：on_llm_request 时把 AstrBot 拼进 system prompt 的技能目录段
   剥离，使 system prompt 不再随技能增删而变化（字节级稳定）。
2. 尾部注入：技能目录改为**当前这条用户消息的一个尾部内容块**注入
   （`extra_user_content_parts`，AstrBot 会把它拼在 `prompt` 之后）。因此
   目录**永远与真实发言共存于同一条消息，且真实发言在前**，任何情况下都
   不会成为"第一条 user 消息"。目录随该消息一并持久化，下一轮即成为前缀的
   一部分，缓存可以复用。检测到技能变化时才追加新的目录块（append-only）。
3. 全会话生效：注入检测在每个会话各自的请求上进行。任何会话（不只是新建
   技能的那个会话）在其下次请求时都会获得最新目录，各自的 system prompt
   前缀都不被破坏。

历史教训（v1 的严重缺陷，务必保留此说明）
------------------------------------------------
v1 把目录作为**独立的持久 user 消息**追加进 `req.contexts`，并在压缩后把它
移动到 system 消息之后（前缀区）。这会踩中 AstrBot 截断器的实现：

`ContextTruncator._ensure_user_message()`
（astrbot/core/agent/context/truncator.py:36-50）在截断结果为空时，会从
原始列表里**找回「第一条 user 消息」**垫在 system 之后：

    first_user = next((m for m in original_messages if m.role == "user"), None)

一旦目录被搬到最前面（或在新会话里成为 contexts 的第一条），它就成了
"第一条 user 消息"。于是截断后唯一幸存的 user 消息变成技能目录，用户真正
说的话被丢弃 —— 模型只看到目录，只能回一句"找我有什么事吗"。

更糟的是 `_save_to_history()`（agent_sub_stages/internal.py:503）保存的是
压缩后的 `run_context.messages`，所以这次错误截断会被**永久写回会话历史**，
表现为"聊着聊着上下文全没了"。

因此 v2 不再让目录单独成条，也**不再打 ContextManager.process 补丁**：
目录始终只是真实发言的尾巴，截断器无论怎么找，"第一条 user 消息"都是真实
发言本身。

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

try:  # AstrBot 4.29+ 提供 ContentPart
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 兼容旧版 AstrBot
    TextPart = None  # type: ignore[assignment]

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

# 目录块首行标记（HTML 注释，WebUI/Markdown 渲染不可见），内嵌 digest。
_CATALOG_MARKER_RE = re.compile(r"^<!-- skill-catalog:([0-9a-f]{16}) -->")

_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()[:16]


def _digest_entries(entries_block: str) -> str:
    return hashlib.sha256(entries_block.encode("utf-8")).hexdigest()[:16]


def _digest_of_catalog_text(text: Any) -> str | None:
    """文本是否为本插件的目录块，是则返回其 digest。"""
    if not isinstance(text, str):
        return None
    match = _CATALOG_MARKER_RE.match(text)
    return match.group(1) if match else None


def _content_blocks(content: Any) -> list[Any]:
    """把消息内容规整成块列表（字符串视作单块）。"""
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _block_text(block: Any) -> Any:
    if isinstance(block, dict):
        return block.get("text")
    return getattr(block, "text", None)


def _is_legacy_catalog_message(item: Any) -> bool:
    """识别 v1 遗留的「独立目录消息」（整条消息就是目录）。

    只认「整条消息以目录标记开头」的情况。v2 的目录是真实发言的尾部块，
    整条消息以真实发言开头，因而不会被误判、不会被清理。
    """
    role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
    if role != "user":
        return False
    content = (
        item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    )
    if isinstance(content, str):
        return _digest_of_catalog_text(content) is not None
    blocks = _content_blocks(content)
    if not blocks:
        return False
    return _digest_of_catalog_text(_block_text(blocks[0])) is not None


def _latest_catalog_digest_in_contexts(contexts: list[Any]) -> tuple[str | None, bool]:
    """在历史里找最新的目录 digest。

    返回 (digest, 是否为独立目录消息)。独立目录消息（v1 遗留）会被顺带识别，
    以便清理；v2 的尾部块返回 (digest, False)。
    """
    for item in reversed(contexts):
        role = (
            item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        )
        if role != "user":
            continue
        content = (
            item.get("content")
            if isinstance(item, dict)
            else getattr(item, "content", None)
        )
        standalone = _is_legacy_catalog_message(item)
        if isinstance(content, str):
            d = _digest_of_catalog_text(content)
            if d:
                return d, standalone
            continue
        for block in reversed(_content_blocks(content)):
            d = _digest_of_catalog_text(_block_text(block))
            if d:
                return d, standalone
    return None, False


def _build_catalog_content(entries_block: str, digest: str, kind: str) -> str:
    """渲染 dsh 风格的目录块（全量快照，name+description+SKILL.md 路径）。"""
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


class _FallbackTextPart:
    """TextPart 不可用时的兜底（对齐 AstrNa identity_metadata 的做法）。"""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text

    def model_dump_for_context(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


def _create_text_part(text: str) -> Any:
    """构造一个会随消息持久化的文本块。

    注意：这里**故意不加** `mark_as_temp()`。目录需要进入历史，下一轮才能
    成为前缀被缓存复用；这正是本插件省 token 的关键。
    """
    if TextPart is None:
        return _FallbackTextPart(text=text)
    return TextPart(text=text)


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
        logger.info("[skill-cache-guard] 插件已加载（技能目录缓存保护）")

    @property
    def _enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    @property
    def _debug(self) -> bool:
        return bool(self.config.get("debug_log", False))

    # -----------------------------------------------------------------------
    # 请求钩子：剥离 system prompt 中的技能目录段，按需把最新目录挂到本轮消息尾部
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
        # 0) 自愈：清掉 v1 遗留的独立目录消息（它们会占住"第一条 user 消息"）
        removed = self._strip_legacy_catalogs(req)

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

        latest_digest, _ = _latest_catalog_digest_in_contexts(contexts)
        if latest_digest == digest:
            return  # 目录已最新：零改动，缓存完全保留
        if not match and latest_digest is None:
            return  # 无技能且从未注入过目录：无需处理

        # 安全约束：目录块只能挂在真实内容（文本/图片/音频）之后。
        # AstrBot 的 assemble_context 按「prompt -> [图片] -> [音频] -> extra 块」
        # 的顺序拼装（core/provider/entities.py:199-214），所以只要三者有其一非空，
        # 目录就一定不是第一个块。若三者全空，目录会顶到最前面，单独成条，
        # 又会踩回 v1 的坑（见模块 docstring），此时放弃注入。
        has_leading_content = bool(
            str(getattr(req, "prompt", "") or "").strip()
            or getattr(req, "image_urls", None)
            or getattr(req, "audio_urls", None)
        )
        if not has_leading_content:
            if self._debug:
                logger.info(
                    "[skill-cache-guard] 本轮无文本/图片/音频，为避免目录单独成条已跳过注入"
                )
            return

        kind = "initial" if latest_digest is None else "update"
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                req.extra_user_content_parts = []
                parts = req.extra_user_content_parts
            parts.append(
                _create_text_part(_build_catalog_content(entries_block, digest, kind))
            )
        except Exception as exc:  # pragma: no cover
            logger.error(f"[skill-cache-guard] 注入目录块失败: {exc}", exc_info=True)
            return

        if self._debug:
            label = "初始目录" if kind == "initial" else "更新目录"
            logger.info(
                f"[skill-cache-guard] 已把{label}挂到本轮消息尾部 "
                f"(digest={digest}, skills={entries_block.count('- **')})"
                + (f", 清理遗留目录 {removed} 条" if removed else "")
            )

    def _strip_legacy_catalogs(self, req: ProviderRequest) -> int:
        """移除 v1 遗留的独立目录消息（自愈清理）。

        这些消息整条都是目录，会占住「第一条 user 消息」的位置，在截断时顶掉
        真实用户消息（见模块 docstring）。v2 的目录是真实发言的尾部块，不受影响。
        """
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return 0
        kept = [item for item in contexts if not _is_legacy_catalog_message(item)]
        removed = len(contexts) - len(kept)
        if removed:
            req.contexts = kept
            logger.info(
                f"[skill-cache-guard] 已清理 {removed} 条遗留的独立技能目录消息"
                "（防止其顶掉真实用户消息）"
            )
        return removed

    async def terminate(self) -> None:
        if SkillCacheGuardPlugin._instance is self:
            SkillCacheGuardPlugin._instance = None
        logger.info("[skill-cache-guard] 插件已卸载")
