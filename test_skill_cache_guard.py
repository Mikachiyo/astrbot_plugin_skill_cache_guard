"""离线测试：astrbot_plugin_skill_cache_guard（main.py）

不依赖 AstrBot 运行环境：
- 用 sys.modules 注入 astrbot.* 桩模块（api/event/provider/star 等）；
- 真实加载 AstrBot 源码中的 message / truncator / compressor / token_counter /
  config / manager（真实的 ContextManager 与截断/压缩逻辑）以及
  skill_manager.build_skills_prompt（真实的技能目录段文本）；
- 再加载插件 main.py，模拟 pipeline 做端到端场景测试。

运行：python test_skill_cache_guard.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

ASTRBOT_SRC = Path(
    r"D:/Program/Astrbot workspace/插件开发/AstrBot_SRC/AstrBot-4.25.1"
)
PLUGIN_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("test-skill-cache-guard")
logging.basicConfig(level=logging.CRITICAL)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 桩模块与真实模块加载
# ---------------------------------------------------------------------------


def _fake_pkg(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod


def _fake_mod(name: str, **attrs) -> types.ModuleType:
    mod = _fake_pkg(name)
    mod.__dict__.update(attrs)
    return mod


def _load_real(dotted: str, relpath: str):
    path = ASTRBOT_SRC / relpath
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


def setup_modules():
    # 包骨架
    astrbot = _fake_mod("astrbot", logger=logger)
    _fake_pkg("astrbot.core")
    _fake_pkg("astrbot.core.agent")
    _fake_pkg("astrbot.core.agent.context")
    _fake_pkg("astrbot.core.skills")
    _fake_pkg("astrbot.core.utils")
    _fake_pkg("astrbot.api")

    # skill_manager 依赖的路径工具（仅导入，不在测试里实例化 SkillManager）
    tmp = str(PLUGIN_DIR / "_test_tmp")
    _fake_mod(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: tmp,
        get_astrbot_plugin_path=lambda: tmp,
        get_astrbot_skills_path=lambda: tmp,
        get_astrbot_temp_path=lambda: tmp,
    )

    # 真实加载 AstrBot 核心模块
    message = _load_real(
        "astrbot.core.agent.message", "astrbot/core/agent/message.py"
    )
    _load_real(
        "astrbot.core.agent.context.truncator",
        "astrbot/core/agent/context/truncator.py",
    )
    _load_real(
        "astrbot.core.agent.context.compressor",
        "astrbot/core/agent/context/compressor.py",
    )
    _load_real(
        "astrbot.core.agent.context.token_counter",
        "astrbot/core/agent/context/token_counter.py",
    )
    _load_real(
        "astrbot.core.agent.context.config",
        "astrbot/core/agent/context/config.py",
    )
    manager = _load_real(
        "astrbot.core.agent.context.manager",
        "astrbot/core/agent/context/manager.py",
    )
    skills = _load_real(
        "astrbot.core.skills.skill_manager",
        "astrbot/core/skills/skill_manager.py",
    )
    astrbot.logger = logger

    # api 桩
    class _Filter:
        @staticmethod
        def on_llm_request(**_kwargs):
            def deco(fn):
                return fn

            return deco

    _fake_mod(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=_Filter(),
    )
    _fake_mod(
        "astrbot.api.provider",
        ProviderRequest=type("ProviderRequest", (), {}),
    )

    class _Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config or {}

    _fake_mod("astrbot.api.star", Context=type("Context", (), {}), Star=_Star)
    sys.modules["astrbot.api"].logger = logger

    return message, manager, skills


message_mod, manager_mod, skills_mod = setup_modules()
Message = message_mod.Message
dump_history = message_mod.dump_messages_with_checkpoints
ContextManager = manager_mod.ContextManager
ContextConfig = sys.modules["astrbot.core.agent.context.config"].ContextConfig
build_skills_prompt = skills_mod.build_skills_prompt
SkillInfo = skills_mod.SkillInfo


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "skill_cache_guard_main", PLUGIN_DIR / "main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["skill_cache_guard_main"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# pipeline 模拟
# ---------------------------------------------------------------------------
BASE_PERSONA = "\n# Persona Instructions\n\n你是小助手。\n"
TOOL_PROMPT = "\n# Tools\n\n你可以使用以下工具。\n"


def make_skill(name: str, desc: str) -> SkillInfo:
    return SkillInfo(
        name=name,
        description=desc,
        path=f"D:\\skills\\{name}\\SKILL.md",
        active=True,
    )


def native_system_prompt(skills: list[SkillInfo]) -> str:
    """模拟 AstrBot 原生组装：persona + 技能段(:467) + tool prompt(:1434)。"""
    sp = BASE_PERSONA
    if skills:
        sp += f"\n{build_skills_prompt(skills)}\n"
    return sp + TOOL_PROMPT


class FakeReq:
    def __init__(self, system_prompt: str, contexts: list[dict]):
        self.system_prompt = system_prompt
        self.contexts = contexts
        self.conversation = object()  # 只需要非 None


async def run_turn(plugin, history, skills, user_text, context_manager=None):
    """模拟一条消息的完整 pipeline：hook -> reset -> process -> 回复 -> 持久化。"""
    req = FakeReq(native_system_prompt(skills), json.loads(json.dumps(history)))
    await plugin.on_llm_request(None, req)

    messages = []
    if req.system_prompt:
        messages.append(Message(role="system", content=req.system_prompt))
    for d in req.contexts:
        messages.append(Message.model_validate(d))
    messages.append(Message(role="user", content=user_text))

    if context_manager is not None:
        messages = await context_manager.process(messages, 0)

    messages.append(Message(role="assistant", content=f"好的，已处理：{user_text[:8]}"))
    new_history = dump_history(messages[1:])  # _save_to_history 跳过首条 system
    return req, messages, new_history


def dumps_msgs(messages) -> list[dict]:
    return dump_history(list(messages))


def catalogs_in(messages) -> list:
    plugin = sys.modules["skill_cache_guard_main"]
    return [
        m
        for m in messages
        if plugin._catalog_digest_of_context_item(m) is not None
    ]


# ---------------------------------------------------------------------------
# 测试框架
# ---------------------------------------------------------------------------
FAILURES: list[str] = []


def check(cond: bool, label: str):
    if cond:
        print(f"  PASS {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 场景测试
# ---------------------------------------------------------------------------
async def main():
    plugin_mod = _load_plugin()
    plugin = plugin_mod.SkillCacheGuardPlugin(context=None, config={})

    s1 = make_skill("weather", "查询城市天气")
    s2 = make_skill("translate", "多语言互译")
    s3 = make_skill("reminder", "定时提醒")

    # ---- T0: 真实技能段可被完整剥离（字节级还原） ----
    print("T0 技能段剥离")
    sp = native_system_prompt([s1, s2])
    match = plugin_mod._SKILLS_SECTION_RE.search(sp)
    check(match is not None, "正则匹配真实 build_skills_prompt 输出")
    stripped = plugin_mod._SKILLS_SECTION_RE.sub("", sp, count=1)
    check(
        stripped == BASE_PERSONA + TOOL_PROMPT,
        "剥离后字节级还原 persona+tool_prompt",
    )
    check("weather" in match.group("entries"), "捕获到技能条目块")

    # ---- T1: 首轮注入 ----
    print("T1 首轮注入初始目录")
    req1, msgs1, hist1 = await run_turn(plugin, [], [s1, s2], "你好")
    check("## Skills" not in req1.system_prompt, "system prompt 已剥离技能段")
    cats = catalogs_in(hist1)
    check(len(cats) == 1, "历史中注入 1 条目录消息")
    content = cats[0]["content"] if isinstance(cats[0], dict) else cats[0].content
    check(
        "weather" in content and "translate" in content and "SKILL.md" in content,
        "目录含名称与 SKILL.md 路径",
    )
    check(
        cats[0].get("role") == "user" if isinstance(cats[0], dict) else True,
        "目录为 user 角色",
    )
    stripped_v1 = req1.system_prompt

    # ---- T2: 次轮稳定（缓存核心断言） ----
    print("T2 次轮字节级稳定")
    req2, msgs2, hist2 = await run_turn(plugin, hist1, [s1, s2], "天气如何")
    check(req2.system_prompt == stripped_v1, "system prompt 与上轮字节一致")
    check(
        dumps_msgs(msgs2[: len(msgs1)]) == dumps_msgs(msgs1),
        "本轮消息序列以上轮完整序列为前缀（缓存全命中）",
    )
    check(len(catalogs_in(hist2)) == 1, "未重复注入目录")

    # ---- T3: 新建技能（同会话） ----
    print("T3 新建技能：尾部追加，前缀不动")
    req3, msgs3, hist3 = await run_turn(plugin, hist2, [s1, s2, s3], "设个提醒")
    check(req3.system_prompt == stripped_v1, "system prompt 仍与首轮字节一致")
    cats3 = catalogs_in(hist3)
    check(len(cats3) == 2, "尾部追加 1 条更新目录（旧目录保留）")
    latest3 = cats3[-1]
    content3 = latest3["content"] if isinstance(latest3, dict) else latest3.content
    check(
        "replaces every earlier" in content3 and "reminder" in content3,
        "更新目录含新技能且声明全量替换",
    )
    check(
        dumps_msgs(msgs3[: len(msgs2)]) == dumps_msgs(msgs2),
        "追加式更新：本轮消息仍以上轮完整序列为前缀",
    )

    # ---- T4: 其他会话 ----
    print("T4 其他会话同样生效")
    req4, msgs4, hist4 = await run_turn(plugin, [], [s1, s2, s3], "hi")
    cats4 = catalogs_in(hist4)
    check(len(cats4) == 1, "新会话注入初始目录")
    c4 = cats4[0]["content"] if isinstance(cats4[0], dict) else cats4[0].content
    check("reminder" in c4, "新会话目录包含最新技能")
    check(req4.system_prompt == stripped_v1, "新会话 system prompt 同样稳定")

    # ---- T5: 真实截断后目录丢失 -> 找回 ----
    print("T5 截断后找回最新目录")
    cat_v2 = None
    for item in reversed(hist3):
        if plugin_mod._catalog_digest_of_context_item(item) is not None:
            cat_v2 = item
            break
    interleaved = []
    for i in range(4):
        interleaved.append({"role": "user", "content": f"第{i}轮" + "聊" * 60})
        interleaved.append({"role": "assistant", "content": "答" * 60})
    long_history = hist3 + interleaved

    cm = ContextManager(ContextConfig(enforce_max_turns=2, max_context_tokens=0))
    req5, msgs5, hist5 = await run_turn(
        plugin, long_history, [s1, s2, s3], "继续", context_manager=cm
    )
    cats5 = catalogs_in(msgs5)
    check(len(cats5) == 1, "截断后只剩 1 条目录")
    check(
        msgs5[1] is cats5[0] and msgs5[0].role == "system",
        "目录位于 system 消息之后（位置 1）",
    )
    d5 = plugin_mod._catalog_digest_of_context_item(cat_v2)
    check(
        plugin_mod._catalog_digest_of_context_item(cats5[0]) == d5,
        "找回的是最新目录",
    )

    # ---- T5b: 目录幸存但未在前缀区 -> 前移 ----
    print("T5b 目录幸存时前移")
    cat_msg = Message(role="user", content=plugin_mod._build_catalog_content("- **a**: x\n  File: `/p/a/SKILL.md`", "aaaaaaaabbbbbbbb", "update"))
    before = [
        Message(role="system", content="sys"),
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
        cat_msg,
        Message(role="user", content="u2"),
    ]
    after = list(before)  # 模拟压缩后目录幸存但不在位置 1
    out = plugin.maintain_catalog(before, after)
    check(out[1] is cat_msg, "目录被移动到 system 之后")
    check(len(catalogs_in(out)) == 1, "目录唯一")

    # ---- T5c: 多目录合并去重 ----
    print("T5c 多目录合并去重")
    old_cat = Message(role="user", content=plugin_mod._build_catalog_content("- **a**: x\n  File: `/p/a/SKILL.md`", "1111111111111111", "initial"))
    new_cat = Message(role="user", content=plugin_mod._build_catalog_content("- **a**: x\n- **b**: y\n  File: `/p/b/SKILL.md`", "2222222222222222", "update"))
    after2 = [
        Message(role="system", content="sys"),
        old_cat,
        Message(role="user", content="u1"),
        new_cat,
        Message(role="user", content="u2"),
    ]
    out2 = plugin.maintain_catalog(after2, after2)
    check(len(catalogs_in(out2)) == 1, "合并为 1 条")
    check(out2[1] is new_cat, "保留最新目录并置于 system 之后")
    check(not any(m is old_cat for m in out2), "旧目录已清理")

    # ---- T6: LLM 摘要压缩吞掉目录 -> 找回 ----
    print("T6 摘要压缩后找回目录")

    class FakeSummaryCompressor:
        def should_compress(self, messages, current_tokens, max_tokens):
            return max_tokens > 0 and current_tokens / max_tokens > 0.82

        async def __call__(self, messages):
            systems = [m for m in messages if m.role == "system"]
            non = [m for m in messages if m.role != "system"]
            return systems + [
                Message(role="user", content="历史摘要：聊了很多。"),
                Message(role="assistant", content="Acknowledged."),
            ] + non[-2:]

    cm6 = ContextManager(
        ContextConfig(max_context_tokens=200, custom_compressor=FakeSummaryCompressor())
    )
    big_history = []
    for i in range(6):
        big_history.append({"role": "user", "content": "问题" * 80})
        big_history.append({"role": "assistant", "content": "回答" * 80})
    req6, msgs6, hist6 = await run_turn(
        plugin, hist3 + big_history, [s1, s2, s3], "再来", context_manager=cm6
    )
    cats6 = catalogs_in(msgs6)
    check(len(cats6) == 1, "摘要压缩后目录被找回")
    check(msgs6[1] is cats6[0], "找回目录位于 system 之后")

    # ---- T7: 无操作快速路径 ----
    print("T7 无压缩时快速路径")
    cm7 = ContextManager(ContextConfig(enforce_max_turns=-1, max_context_tokens=0))
    sample = [
        Message(role="system", content="sys"),
        Message(role="user", content="u"),
    ]
    out7 = await cm7.process(sample, 0)
    check(out7 is sample, "无压缩时返回原对象（零改动）")

    # ---- T8: 全部技能删除 -> 墓碑更新 ----
    print("T8 全部技能删除")
    req8, msgs8, hist8 = await run_turn(plugin, hist3, [], "技能呢")
    check(req8.system_prompt == BASE_PERSONA + TOOL_PROMPT, "无技能时 prompt 不变")
    cats8 = catalogs_in(hist8)
    c8 = cats8[-1]["content"] if isinstance(cats8[-1], dict) else cats8[-1].content
    check("(none)" in c8 and "No skills are currently available" in c8, "注入空目录墓碑")

    # ---- T9: 持久化往返幂等 ----
    print("T9 持久化往返幂等")
    req9, msgs9, hist9 = await run_turn(plugin, hist3, [s1, s2, s3], "再来一次")
    check(len(catalogs_in(hist9)) == 2, "重启后同技能状态不重复注入")

    # ---- T10: 格式变化放行 ----
    print("T10 格式变化放行原生")
    weird = BASE_PERSONA + "\n## Skills\n\n这个格式插件不认识。\n" + TOOL_PROMPT
    req10 = FakeReq(weird, [])
    await plugin.on_llm_request(None, req10)
    check(req10.system_prompt == weird, "不认识格式时不改 system prompt")
    check(len(req10.contexts) == 0, "不认识格式时不追加目录")

    # ---- T11: 插件禁用 ----
    print("T11 禁用后完全放行")
    plugin_off = plugin_mod.SkillCacheGuardPlugin(context=None, config={"enable": False})
    req11 = FakeReq(native_system_prompt([s1]), [])
    await plugin_off.on_llm_request(None, req11)
    check("## Skills" in req11.system_prompt, "禁用时保留原生技能段")
    check(len(req11.contexts) == 0, "禁用时无注入")
    plugin_mod.SkillCacheGuardPlugin._instance = plugin  # 还原

    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
