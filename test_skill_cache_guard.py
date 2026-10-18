"""离线测试：astrbot_plugin_skill_cache_guard（main.py）

不依赖 AstrBot 运行环境：
- 用 sys.modules 注入 astrbot.* 桩模块（api/event/provider/star 等）；
- 真实加载 AstrBot 源码中的 message / truncator / compressor / token_counter /
  config / manager（真实的 ContextManager 与截断/压缩逻辑）以及
  skill_manager.build_skills_prompt（真实的技能目录段文本）；
- 再加载插件 main.py，模拟 pipeline 做端到端场景测试。

运行：
    python test_skill_cache_guard.py
可用 ASTRBOT_SRC 指定 AstrBot 源码/安装路径；未指定时按常见位置自动探测
（兼容 site-packages 安装布局与源码仓库布局）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

_CANDIDATES = [
    os.environ.get("ASTRBOT_SRC", ""),
    "/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages",
    "/root/.local/share/uv/tools/astrbot/lib/python3.12/site-packages/astrbot",
    r"D:/Program/Astrbot workspace/插件开发/AstrBot_SRC/AstrBot-4.25.1",
]


def _resolve_src() -> Path:
    """返回包含 core/ 的那一层目录（即 astrbot 包目录）。"""
    for raw in _CANDIDATES:
        if not raw:
            continue
        base = Path(raw)
        for cand in (base, base / "astrbot"):
            if (cand / "core" / "agent" / "message.py").is_file():
                return cand
    raise SystemExit(
        "找不到 AstrBot 源码。请设置 ASTRBOT_SRC 指向 astrbot 包目录"
        "（含 core/agent/message.py）。"
    )


ASTRBOT_PKG = _resolve_src()

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
    path = ASTRBOT_PKG / relpath
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


def setup_modules():
    # 包骨架
    _fake_mod("astrbot", logger=logger)
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
        get_astrbot_builtin_plugin_path=lambda: tmp,
        get_astrbot_root=lambda: tmp,
        get_astrbot_config_path=lambda: tmp,
    )

    # compressor 会 import ...provider.modalities；截断路径用不到它，桩掉即可
    _fake_pkg("astrbot.core.provider")
    _fake_mod(
        "astrbot.core.provider.modalities",
        sanitize_contexts_by_modalities=lambda contexts, modalities: (
            contexts,
            {},
        ),
        log_context_sanitize_stats=lambda *a, **k: None,
    )

    # 真实加载 AstrBot 核心模块
    message = _load_real("astrbot.core.agent.message", "core/agent/message.py")
    _load_real(
        "astrbot.core.agent.context.truncator", "core/agent/context/truncator.py"
    )
    # 注意加载顺序：compressor/manager 依赖 token_counter、round_utils，须先加载
    _load_real(
        "astrbot.core.agent.context.token_counter",
        "core/agent/context/token_counter.py",
    )
    _load_real(
        "astrbot.core.agent.context.compressor", "core/agent/context/compressor.py"
    )
    _load_real(
        "astrbot.core.agent.context.round_utils",
        "core/agent/context/round_utils.py",
    )
    _load_real("astrbot.core.agent.context.config", "core/agent/context/config.py")
    manager = _load_real(
        "astrbot.core.agent.context.manager", "core/agent/context/manager.py"
    )
    skills = _load_real(
        "astrbot.core.skills.skill_manager", "core/skills/skill_manager.py"
    )
    sys.modules["astrbot"].logger = logger

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
        path=f"/skills/{name}/SKILL.md",
        active=True,
    )


def native_system_prompt(skills: list[SkillInfo]) -> str:
    """模拟 AstrBot 原生组装：persona + 技能段(:467) + tool prompt(:1434)。"""
    sp = BASE_PERSONA
    if skills:
        sp += f"\n{build_skills_prompt(skills)}\n"
    return sp + TOOL_PROMPT


class FakeEvent:
    def __init__(self, umo: str = "test:session"):
        self.unified_msg_origin = umo


class FakeReq:
    def __init__(self, system_prompt: str, contexts: list[dict], prompt: str = "当前发言"):
        self.system_prompt = system_prompt
        self.contexts = contexts
        self.extra_user_content_parts: list = []
        self.prompt = prompt
        self.image_urls: list = []
        self.audio_urls: list = []
        self.conversation = object()  # 只需要非 None


def build_user_message(user_text: str, extra_parts: list) -> Message:
    """模拟 entities.assemble_context：prompt 在前，extra parts 追加在后。"""
    if not extra_parts:
        return Message(role="user", content=user_text)
    return Message(role="user", content=[{"type": "text", "text": user_text}] + list(extra_parts))


def dump_persisted(messages) -> list[dict]:
    """模拟 _save_to_history：跳过首条 system，过滤标记为 _no_save 的消息和内容块。

    注意：v2 的目录块**故意不带** `_no_save`，必须随历史落库 —— 下一轮它成为
    前缀的一部分，缓存才能复用，这正是本插件省 token 的关键。
    """
    kept = []
    skipped_initial_system = False
    for m in messages:
        if m.role == "system" and not skipped_initial_system:
            skipped_initial_system = True
            continue
        if m.role in ("assistant", "user") and getattr(m, "_no_save", False):
            continue
        if isinstance(m.content, list):
            blocks = [
                p for p in m.content
                if not getattr(p, "_no_save", False)
                and not (isinstance(p, dict) and p.get("_no_save"))
            ]
            if not blocks:
                continue
            m = Message(role=m.role, content=[
                p.model_dump_for_context() if hasattr(p, "model_dump_for_context") else p
                for p in blocks
            ])
        kept.append(m)
    return dump_history(kept)


def dumps_msgs(messages) -> list[dict]:
    """把消息列表转成可比较的 dict 列表。"""
    return dump_history([m for m in messages])


async def run_turn(plugin, history, skills, user_text, context_manager=None,
                   umo="test:session"):
    """模拟一条消息的完整 pipeline：hook -> 组装 -> 压缩 -> 回复 -> 持久化。"""
    req = FakeReq(native_system_prompt(skills), json.loads(json.dumps(history)))
    await plugin.on_llm_request(FakeEvent(umo), req)

    messages = []
    if req.system_prompt:
        messages.append(Message(role="system", content=req.system_prompt))
    for d in req.contexts:
        messages.append(Message.model_validate(d))
    messages.append(build_user_message(user_text, req.extra_user_content_parts))

    if context_manager is not None:
        messages = await context_manager.process(messages, 0)

    messages.append(Message(role="assistant", content=f"好的，已处理：{user_text[:8]}"))
    return req, messages, dump_persisted(messages)


def catalogs_in_contexts(req) -> list:
    plugin = sys.modules["skill_cache_guard_main"]
    return [
        i for i in (req.contexts or [])
        if plugin._catalog_digest_of_context_item(i) is not None
    ]


def catalogs_in(messages) -> list:
    plugin = sys.modules["skill_cache_guard_main"]
    return [m for m in messages if plugin._catalog_digest_of_message(m) is not None]


def texts_of(messages) -> str:
    parts = []
    for m in messages:
        c = m.content
        if isinstance(c, list):
            parts.append(" ".join(
                p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
                for p in c
            ))
        else:
            parts.append(str(c or ""))
    return "\n".join(parts)


def user_messages(messages) -> list:
    return [m for m in messages if m.role == "user"]


def first_text_of(m) -> str:
    """取消息的第一个文本块（对齐 entities.assemble_context 的拼装顺序）。"""
    c = m.content
    if isinstance(c, list):
        for p in c:
            txt = p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
            if txt:
                return str(txt)
        return ""
    return str(c or "")


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
    print(f"AstrBot 源码: {ASTRBOT_PKG}")
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
    check(stripped == BASE_PERSONA + TOOL_PROMPT, "剥离后字节级还原 persona+tool_prompt")
    check("weather" in match.group("entries"), "捕获到技能条目块")

    # ---- C1: 目录挂在当前消息尾部，与真实发言同条，不单独成条 ----
    print("C1 目录作为真实发言的尾部块注入（不单独成条）")
    req1, msgs1, hist1 = await run_turn(plugin, [], [s1, s2], "你好")
    check("## Skills" not in req1.system_prompt, "system prompt 已剥离技能段")
    check(len(req1.extra_user_content_parts) == 1, "挂上 1 个目录块")
    check(len(catalogs_in_contexts(req1)) == 0, "req.contexts 中没有独立目录消息")
    txt = texts_of(msgs1)
    check("weather" in txt and "SKILL.md" in txt, "目录随用户消息下发到模型")
    # 关键：本轮用户消息的第一个块必须是真实发言
    cur = user_messages(msgs1)[-1]
    check(first_text_of(cur) == "你好", "本轮消息以真实发言开头（目录只是它的尾巴）")
    check("skill-catalog" in str(cur.content), "同一条消息里既有真实发言也有目录")

    # ---- C3: 目录随历史持久化（这是省 token 的关键）----
    print("C3 目录随历史持久化，供下一轮缓存复用")
    blob = json.dumps(hist1, ensure_ascii=False)
    check("<!-- skill-catalog:" in blob, "目录已进入历史（下一轮可作前缀复用）")
    check("你好" in blob, "落库历史包含用户原文")
    check(not any(plugin_mod._is_legacy_catalog_message(m) for m in hist1),
          "历史里没有被误判为「独立目录消息」的条目")

    # ---- C2-bug: 复现 v1 的致命缺陷（证明测试能捕捉到该 bug） ----
    print("C2-bug 复现：目录被搬到前缀后会顶掉真实用户消息")
    poisoned_cat = plugin_mod._build_catalog_content(
        "- **weather**: 查询城市天气\n  File: `/skills/weather/SKILL.md`",
        "deadbeefdeadbeef", "initial")
    poisoned = [
        Message(role="system", content="sys-persona"),
        Message(role="user", content=poisoned_cat),     # v1 的 maintain_catalog 搬到这里
        Message(role="user", content="REAL-USER-TEXT"),
        Message(role="assistant", content="a"),
        Message(role="user", content="后续1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="后续2"),
        Message(role="assistant", content="a2"),
    ]
    cm_drop = ContextManager(ContextConfig(enforce_max_turns=1, truncate_turns=2))
    out_bug = await cm_drop.process(list(poisoned), 0)
    bug_txt = texts_of(out_bug)
    check("REAL-USER-TEXT" not in bug_txt and "skill-catalog" in bug_txt,
          "确认缺陷：真实消息丢失、目录被复活为唯一 user 消息")

    # ---- C2-fix: 新版目录与真实发言同条，截断后真话仍在 ----
    print("C2-fix 新版：截断后真实用户消息得以保留")
    req_fix = FakeReq(native_system_prompt([s1]), [])
    await plugin.on_llm_request(FakeEvent(), req_fix)
    cur_block = {"role": "user", "content": [
        {"type": "text", "text": "REAL-USER-TEXT"},
        *[p.model_dump_for_context() for p in req_fix.extra_user_content_parts],
    ]}
    fixed_msgs = [Message(role="system", content=req_fix.system_prompt)]
    fixed_msgs += [Message.model_validate(cur_block)]
    fixed_msgs += [
        Message(role="assistant", content="a1"),
        Message(role="user", content="后续1"),
        Message(role="assistant", content="a2"),
        Message(role="user", content="后续2"),
        Message(role="assistant", content="a3"),
    ]
    check(not plugin_mod._is_legacy_catalog_message(cur_block),
          "新版消息不被识别为独立目录消息")
    out_fix = await cm_drop.process(list(fixed_msgs), 0)
    fix_txt = texts_of(out_fix)
    check("REAL-USER-TEXT" in fix_txt, "截断后真实用户消息仍存在")

    # ---- C4: 同会话多轮，目录不重复累积 ----
    print("C4 多轮对话目录只在变化时追加")
    hist = []
    for i, (txt, sk) in enumerate([("第一句", [s1]), ("第二句", [s1]), ("第三句", [s1, s2])]):
        req_i, msgs_i, hist = await run_turn(plugin, hist, sk, txt)
        blob = json.dumps(hist, ensure_ascii=False)
        check(txt in blob, f"第{i+1}轮原文在库")
    # 技能只在第三轮变化，所以历史里应恰好 2 个目录块（第一轮 initial + 第三轮 update）
    n = json.dumps(hist, ensure_ascii=False).count("<!-- skill-catalog:")
    check(n == 2, f"目录块数=2（首次+一次变化），实际 {n}")
    check(not any(plugin_mod._is_legacy_catalog_message(m) for m in hist),
          "历史中没有独立目录消息（不会顶掉真话）")

    # ---- C5: system prompt 字节级稳定（缓存收益保留） ----
    print("C5 system prompt 字节级稳定")
    reqA, _, _ = await run_turn(plugin, [], [s1, s2], "a")
    reqB, _, _ = await run_turn(plugin, [], [s1, s2, s3], "b")
    check(reqA.system_prompt == reqB.system_prompt == BASE_PERSONA + TOOL_PROMPT,
          "技能增删后 system prompt 仍字节一致")

    # ---- C6: 缓存复用（插件存在的意义，必须量化验证）----
    print("C6 目录能带来前缀复用（省 token 效果）")

    def _prefix_len(x: str, y: str) -> int:
        n = min(len(x), len(y))
        i = 0
        while i < n and x[i] == y[i]:
            i += 1
        return i

    async def _wire(hist, user_text, skills, use_plugin):
        if use_plugin:
            return await run_turn(plugin, hist, skills, user_text)
        # 原生对照：技能段留在 system prompt
        req = FakeReq(native_system_prompt(skills), json.loads(json.dumps(hist)))
        msgs = [Message(role="system", content=req.system_prompt)]
        msgs += [Message.model_validate(d) for d in req.contexts]
        msgs.append(Message(role="user", content=user_text))
        msgs.append(Message(role="assistant", content="收到"))
        return req, msgs, dump_persisted(msgs)

    async def _measure(use_plugin, change_at):
        hist = []
        prev = None
        fresh = 0
        for t in range(1, 7):
            sk = [s1, s2] if (change_at is None or t < change_at) else [s1, s2, s3]
            req, msgs, hist = await _wire(hist, f"第{t}句话", sk, use_plugin)
            wire = json.dumps(
                {"sp": req.system_prompt, "msgs": dumps_msgs(msgs)}, ensure_ascii=False
            )
            if prev is None:
                fresh += len(wire)
            else:
                fresh += len(wire) - _prefix_len(prev, wire)
            prev = wire
        return fresh

    # 场景1：技能不变，插件应至少不比原生差
    nat_keep = await _measure(False, None)
    fix_keep = await _measure(True, None)
    check(fix_keep <= nat_keep,
          f"技能不变时不比原生差（插件 {fix_keep} vs 原生 {nat_keep}）")

    # 场景2：技能变化，插件应明显更省
    nat_chg = await _measure(False, 3)
    fix_chg = await _measure(True, 3)
    check(fix_chg < nat_chg,
          f"技能变化时比原生省（插件 {fix_chg} vs 原生 {nat_chg}）")

    # ---- T7: 无压缩时快速路径 ----
    print("T7 无压缩时快速路径")
    cm7 = ContextManager(ContextConfig(enforce_max_turns=-1, max_context_tokens=0))
    sample = [Message(role="system", content="sys"), Message(role="user", content="u")]
    out7 = await cm7.process(sample, 0)
    check(out7 is sample, "无压缩时返回原对象（零改动）")

    # ---- T10: 格式变化放行 ----
    print("T10 格式变化放行原生")
    weird = BASE_PERSONA + "\n## Skills\n\n这个格式插件不认识。\n" + TOOL_PROMPT
    req10 = FakeReq(weird, [])
    await plugin.on_llm_request(FakeEvent(), req10)
    check(req10.system_prompt == weird, "不认识格式时不改 system prompt")
    check(len(req10.contexts) == 0 and not req10.extra_user_content_parts,
          "不认识格式时不注入")

    # ---- T11: 插件禁用 ----
    print("T11 禁用后完全放行")
    plugin_off = plugin_mod.SkillCacheGuardPlugin(context=None, config={"enable": False})
    req11 = FakeReq(native_system_prompt([s1]), [])
    await plugin_off.on_llm_request(FakeEvent(), req11)
    check("## Skills" in req11.system_prompt, "禁用时保留原生技能段")
    check(not req11.extra_user_content_parts, "禁用时无注入")
    plugin_mod.SkillCacheGuardPlugin._instance = plugin  # 还原

    # ---- T12: 无技能不注入 ----
    print("T12 无技能时不注入")
    req12 = FakeReq(native_system_prompt([]), [])
    await plugin.on_llm_request(FakeEvent(), req12)
    check(not req12.extra_user_content_parts, "无技能时不产生额外块")

    # ---- T12b: 纯图片消息也要注入（目录挂在 [图片] 占位之后）----
    print("T12b 纯图片消息仍注入，且目录不是第一个块")
    req12b = FakeReq(native_system_prompt([s1]), [], prompt="")
    req12b.image_urls = ["file:///tmp/x.png"]
    await plugin.on_llm_request(FakeEvent(), req12b)
    check(len(req12b.extra_user_content_parts) == 1, "纯图片消息也挂上目录块")

    # ---- T12c: 无文本/图片/音频时跳过（避免目录单独成条）----
    print("T12c 空消息跳过注入（防止目录单独成条）")
    req12c = FakeReq(native_system_prompt([s1]), [], prompt="")
    await plugin.on_llm_request(FakeEvent(), req12c)
    check(not req12c.extra_user_content_parts, "空消息不注入（守住安全底线）")

    # ---- T13: 清理 v1 遗留的独立目录消息（自愈）----
    print("T13 清理 v1 遗留的独立目录消息")
    legacy = plugin_mod._build_catalog_content(
        "- **x**: y\n  File: `/s/x/SKILL.md`", "1111222233334444", "initial")
    req13 = FakeReq(native_system_prompt([s1]), [
        {"role": "user", "content": legacy},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "真实旧发言"},
    ])
    await plugin.on_llm_request(FakeEvent(), req13)
    check(all(not plugin_mod._is_legacy_catalog_message(i) for i in req13.contexts),
          "遗留的独立目录消息已被清掉")
    check(any("真实旧发言" in json.dumps(i, ensure_ascii=False)
              for i in req13.contexts), "真实历史发言未被误删")

    # ---- T13b: 不得误删「真实发言+目录尾巴」的 v2 形态 ----
    print("T13b 不误删 v2 形态（真实发言+目录尾巴）")
    v2_item = {"role": "user", "content": [
        {"type": "text", "text": "真实发言"},
        {"type": "text", "text": legacy},
    ]}
    req13b = FakeReq(native_system_prompt([s1]), [v2_item])
    await plugin.on_llm_request(FakeEvent(), req13b)
    check(len(req13b.contexts) == 1, "v2 形态的消息被保留（未误删）")

    # ---- T14: 无猴子补丁 ----
    print("T14 不再打 ContextManager 补丁")
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    # 只检查可执行代码：去掉 docstring 中的说明性提及
    code_part = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
    check("ContextManager.process" not in code_part.replace(
        "# 因此本版本**禁止**再向 req.contexts 写入目录", ""),
        "main.py 代码中不再出现 ContextManager.process 补丁")
    check("_install_process_patch" not in code_part, "不再安装 process 补丁")
    check("contexts.append" not in code_part, "不再向 contexts append 目录消息")

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
