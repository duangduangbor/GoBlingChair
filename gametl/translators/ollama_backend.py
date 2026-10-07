# -*- coding: utf-8 -*-
"""本地翻译后端：Ollama HTTP API。

架构（v1.1）::

    预过滤 → 去重 → 分块 → 并发请求 → 逐块校验 → 结果分发

相比 v1.0 的「逐条串行」，这里做了四件事：

1. **预过滤** —— 纯符号 / 纯数字 / 已是中文的条目直接跳过，不浪费推理。
2. **去重** —— 同一段原文只翻译一次，结果分发给所有出现位置。
   RPG Maker 的数据库条目会在多个 Map / CommonEvents 里重复出现，
   实测重复率可达三到五成。
3. **分块批量** —— 一次请求翻译多条，共享固定的 system prompt 前缀。
   翻译任务的固定开销（系统提示 + 模板）往往是正文长度的数倍，
   批量能把这份开销摊薄到 N 分之一。
4. **并发** —— 线程池同时打多个请求，配合服务端 OLLAMA_NUM_PARALLEL
   的多槽位，让 GPU 真正跑满（单槽时并发请求只会排队，白等）。

质量保障：

- 每次请求返回后立即做「第一层硬校验」（见 ``core/validate.py``）；
- 批量请求若 JSON 解析失败或条数对不上，**自动降级为逐条重发**；
- 单条失败不影响整批；失败条目保持 ``translated=None``，续传时会重试。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

from ..core.validate import check_immediate, has_kana, already_in_target
from .glossary import Glossary

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"


#: 提示词回声的指纹（2026-10-08 CQ2 事故换来的）。
#:
#: 小模型偶尔会在 JSON 里"串行"：先写译文，再把自己的指令/收尾语也写进同一个
#: 字符串值。CQ2 实测 16 条中招，例如
#: ``ACHV036TEXT`` 原文 ``Acquired the Pumpkin Costume.`` →
#: ``获得了南瓜服装。}``` 提示：仅输出 JSON 对象…`` 输出：{``
#: 这类值会原样回填进游戏 —— 又长又脏，还会把 ``{`` ``}`` 结构符号带进表里。
#:
#: 兜底交给 ``_clean`` 去"猜"太危险（可能把正常译文也截断），所以在**批量校验**
#: 这一步直接判定整批作废：失败条目保持 ``translated=None``，续传时会重试。
_LEAK_MARKERS: tuple = (
    "```", "JSON", "json 对象", "markdown", "占位符", "不要输出", "不需要任何解释",
    "译文不含换行符", "译文中没有", "无换行符", "保持原样输出", "键与输入",
    "翻译结果", "输出结果如下", "最终输出如下", "以下是翻译", "好的，我将",
    "好的，已", "按照您的要求", "按您的要求", "已按要求",
)


def looks_leaked(value: str, original: str) -> str:
    """判断这条译文是不是"模型把提示词/收尾语也写进来了"。返回原因或空串。

    判据有两条，任一命中即算污染：

    1. 命中 ``_LEAK_MARKERS`` —— 正常译文里不会出现这些词；
    2. 长度失控 —— 超过原文长度的 ``_LEAK_RATIO`` 倍且绝对值也够大。
       中文一般比英文短，所以"译文比原文长好几倍"本身就是异常信号
       （CQ2 实测 ACHV036TEXT 那条比值 17.8）。
    """
    t = (value or "").strip()
    if not t:
        return ""
    low = t.lower()
    for m in _LEAK_MARKERS:
        if m.lower() in low:
            return "疑似提示词回声（命中 %r）" % m
    if len(original) >= 8 and len(t) > max(64, len(original) * 6):
        return "疑似提示词回声（长度 %d，原文 %d）" % (len(t), len(original))
    return ""


SYSTEM_PROMPT = """你是一名专业的游戏本地化译者，负责将游戏文本翻译为目标语言。

严格遵守以下规则：
1. 只输出译文本身，不要任何解释、引号、前缀或额外说明。
2. 文本中的【0】【1】这类全角括号数字是受保护占位符，必须原样保留，不要翻译、不要改变其编号或位置。
3. 保持原文的语气、人设与情感色彩。对话要口语化、自然，像真人说话。
4. 游戏专有名词、人名、地名按术语表翻译；术语表没有的，保持音译一致性。
5. 不要添加原文没有的内容，不要漏译。
6. 若原文已是目标语言或无需翻译，原样返回。
7. 译文不要出现换行符。"""

# 精简版：同样约 7 条约束，但压缩了措辞。
# 固定前缀是每条请求都要重新 prefill 的部分，在 7.7 万条的规模下，
# 省下来的每一个 token 都会被放大数万倍。
SYSTEM_PROMPT_SLIM = """你是游戏本地化译者，把给定文本译成目标语言。

规则：
1. 只输出译文，不要解释、引号或前缀。
2. 【0】【1】这类全角括号数字是占位符，必须原样保留且不改变编号。
3. 保持原文语气，对话要口语化。
4. 人名地名按术语表；术语表没有的保持音译一致。
5. 不增删内容，不漏译。
6. 已是目标语言或无需翻译的，原样返回。
7. 译文不含换行符。"""

BATCH_PROMPT = """把下面 JSON 里每个值翻译成目标语言。

要求：
1. 只输出 JSON 对象，键与输入完全一致，值是译文。
2. 不要输出任何解释、说明或 markdown 代码块。
3. 【0】【1】这类占位符原样保留。
4. 保持原文语气，对话口语化；术语按下方对照。
5. 译文不含换行符。"""

# ---- 翻译专用模型（腾讯混元 Hy-MT / Hunyuan-MT 系列）的官方提示词 ----
#
# 关键差异：这类模型**没有默认 system prompt**，全部约束必须写在用户内容里，
# 否则输出会带解释文字。模板取自官方 README。
HYMT_PROMPT = ("将以下文本翻译成{target_lang}，"
               "注意只需要输出翻译后的结果，不要额外解释：\n\n{source_text}")

HYMT_PROMPT_WITH_TERMS = ("将以下文本翻译成{target_lang}，"
                          "注意只需要输出翻译后的结果，不要额外解释。\n"
                          "参考下面的翻译：\n{terms}\n\n{source_text}")

HYMT_BATCH_PROMPT = """将下面 JSON 里每个值翻译成{target_lang}，注意只需要输出翻译后的结果，不要额外解释。

要求：
1. 只输出 JSON 对象，键与输入完全一致，值是译文。
2. 不要输出任何解释、说明或 markdown 代码块。
3. 【0】【1】这类占位符原样保留。
4. 译文不含换行符。"""


_TRANSLATION_MODEL_HINTS = ("hy-mt", "hymt", "hy_mt",
                            "hunyuan-mt", "hunyuan_mt", "translategemma")


def detect_prompt_style(model_file: str) -> str:
    """按模型文件名判断该用哪套提示词。"""
    low = (model_file or "").lower()
    if any(h in low for h in _TRANSLATION_MODEL_HINTS):
        return "hymt"
    return "generic"


# ---------------------------------------------------------------- 配置

@dataclass
class TranslateOptions:
    """并发/批量行为的开关集合。

    这些值由「性能模式」（profiles.py）统一提供，也可以单独覆盖。
    """
    concurrency: int = 4          # 并发请求数（≈ 服务端槽位数）
    batch_size: int = 8           # 单个请求里翻译几条
    use_batch: bool = True        # 关闭则退化为「并发 + 单条」
    # 实测结论：短文本用批量**反而更慢** —— JSON 结构开销（约 8 token/条）
    # 比译文本身还长，生成量翻好几倍。所以只有平均原文够长时才启用批量。
    batch_min_len: float = 32.0   # 平均原文长度低于此值就自动关闭批量。
                                  # 实测（1.8B·并发4·400 条唯一语料·均长 21.7 字）：
                                  #   单条 22.67 条/秒  vs  批量8 14.48 条/秒
                                  # 批量慢 36% —— JSON 结构开销（约 8 token/条）
                                  # 比译文本身还长，只有长文本才摊得平。
    slim_prompt: bool = True      # 使用精简版 system prompt
    prompt_style: str = "auto"    # auto / generic / hymt
    model_file: str = ""          # models/ 里的原始文件名（用于判断风格）
    keep_alive: str = "30m"       # 让模型常驻显存，避免反复重载
    num_predict_cap: int = 1024   # 单次生成的 token 上限
    verify: bool = True           # 是否做第一层即时校验
    max_retries: int = 3
    skip_useless: bool = True     # 是否预过滤无需翻译的条目

    # --- 上下文一致性（v2.5）---
    # 批量模式（默认）此前把提取器认出来的说话人/场景**直接丢掉**了：
    # `_build_batch_prompt` 拿到 (idx, text, context) 三元组却只用 text。
    # 打开后把说话人写进提示词，并给每批附上前 N 条的原文当「只读上文」——
    # 让模型能消解代词与称呼（"它"/"那家伙"到底指谁）。
    # 代价：每条请求多几十到几百 token（前缀会被 KV 缓存复用，实测吞吐几乎不变）。
    inject_context: bool = True
    context_overlap: int = 2      # 每批附带的前置原文条数（0 = 关闭上文）


# ---------------------------------------------------------------- 预过滤

# 只要串里出现字母 / 假名 / 汉字，就认为「有内容」
_MEANINGFUL = re.compile(r"[A-Za-z\u3040-\u30ff\u4e00-\u9fff]")
# 纯中文（含中文标点、数字、空白）—— 已经是中文就没必要再翻
_ALREADY_CJK = re.compile(
    r"^[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef0-9A-Za-z\s]*$")
# 真正的中日韩表意文字（用来确认「确实有汉字」）
_HANZI = re.compile(r"[\u4e00-\u9fff]")


def should_skip(text: str, target_lang: str = "简体中文") -> bool:
    """判断一段文本是否无需翻译。

    保守策略 —— **宁可多翻，不可漏翻**：
    只有明显没有语言内容时（空串、单字符、纯符号数字、已是目标语言）才跳过。
    英文串一律不跳（游戏里 "Potion" 这类是需要翻译的）。

    ``target_lang`` 控制「已是目标语言」怎么判：默认简体中文（有汉字即算）；
    翻成日语则「出现假名才算」、翻成韩语则「出现谚文才算」、翻成英语等
    拉丁语言则「以字母为主才算」。未知语言一律不跳过（照常翻译）。
    """
    t = (text or "").strip()
    if not t:
        return True
    if len(t) <= 1:
        return True
    if not _MEANINGFUL.search(t):
        return True                                  # 纯符号 / 纯数字
    if already_in_target(t, target_lang):
        return True                                  # 已是目标语言
    return False


# ---------------------------------------------------------------- 主体


class OllamaTranslator:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        temperature: float = 0.2,
        timeout: int = 180,
        max_retries: int = 3,
        options: Optional[TranslateOptions] = None,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.opt = options or TranslateOptions()

        # 提示词风格：翻译专用模型（混元 Hy-MT 等）没有 system prompt，
        # 约束必须写进用户内容里，否则输出会拖着解释文字回来。
        style = self.opt.prompt_style
        if style == "auto":
            style = detect_prompt_style(self.opt.model_file or model)
        self.prompt_style = style

        self._cancel = threading.Event()
        self.stats: dict[str, int] = {
            "skipped": 0, "unique": 0, "requests": 0,
            "batched": 0, "degraded": 0, "retried": 0, "failed": 0,
            "batch_off": 0, "unverified": 0, "ctx": 0,
        }

    # ---------------- 基础 ----------------

    def cancel(self) -> None:
        """请求中止（线程池里的任务会在下一个检查点退出）。"""
        self._cancel.set()

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(
                    f"{self.host}/api/version", timeout=5) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def list_models(self) -> list:
        try:
            with urllib.request.urlopen(
                    f"{self.host}/api/tags", timeout=10) as r:
                data = json.loads(r.read())
                return [m["name"] for m in data.get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def generate(self, prompt: str, system: str = "",
                 format_json: bool = False, temperature: float = 0.1,
                 num_predict: int = 1024) -> str:
        """低层通用生成 —— 给「术语抽取」这类辅助任务复用。

        与 ``translate_one`` 的区别：不做译文清理、不做硬校验，只把模型
        原始输出交回调用方。术语筛选需要的是模型的**判断**，不是译文。
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "keep_alive": self.opt.keep_alive,
            "options": {"temperature": temperature, "top_p": 0.9,
                        "num_predict": num_predict},
        }
        if format_json:
            payload["format"] = "json"
        data = self._post(payload)
        self.stats["requests"] += 1
        return (data.get("response") or "").strip()

    def _system(self) -> str:
        return SYSTEM_PROMPT_SLIM if self.opt.slim_prompt else SYSTEM_PROMPT

    # ---------------- 单条 ----------------

    def _build_prompt(self, text: str, glossary: Glossary,
                      context: dict, target_lang: str) -> str:
        lines: list = []
        if context.get("speaker"):
            lines.append(f"说话人：{context['speaker']}")
        if context.get("scene"):
            lines.append(f"场景：{context['scene']}")
        terms = glossary.relevant_for(text)
        if terms:
            lines.append("术语（必须遵守）："
                         + "；".join(f"{k} => {v}" for k, v in terms.items()))
        lines.append(f"目标语言：{target_lang}")
        lines.append("")
        lines.append("原文：")
        lines.append(text)
        lines.append("")
        lines.append("请只输出译文：")
        return "\n".join(lines)

    def _build_single(self, text: str, glossary: Glossary,
                      context: dict, target_lang: str) -> tuple:
        """构造单条请求的 (prompt, system)。

        翻译专用模型走官方模板（无 system），通用模型走「system 规则 + 模板」。
        """
        if self.prompt_style == "hymt":
            terms = glossary.relevant_for(text)
            if terms:
                term_str = "；".join(f"{k} 翻译成 {v}" for k, v in terms.items())
                return HYMT_PROMPT_WITH_TERMS.format(
                    target_lang=target_lang, terms=term_str,
                    source_text=text), ""
            return HYMT_PROMPT.format(target_lang=target_lang,
                                      source_text=text), ""
        return (self._build_prompt(text, glossary, context, target_lang),
                self._system())

    def translate_one(
        self,
        text: str,
        glossary: Optional[Glossary] = None,
        context: Optional[dict] = None,
        target_lang: str = "简体中文",
        allow_partial: bool = False,
    ) -> str:
        """翻译单条文本，带重试 + 即时校验。

        Args:
            allow_partial: 重试耗尽且校验仍不过时，**返回最后一次的输出**
                而不是抛错（调用方需自行判断是否通过校验）。
                这把「翻不动」的条目从「永远留空、每次重跑都再失败一遍」
                变成「有译文、被标记待复核」—— 否则工程永远到不了 100%，
                用户会一直觉得程序在重复劳动。
        """
        glossary = glossary or Glossary()
        context = context or {}
        prompt, system = self._build_single(text, glossary, context, target_lang)

        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "keep_alive": self.opt.keep_alive,
            "options": {
                "temperature": self.temperature,
                "top_p": 0.9,
                "num_predict": min(max(256, len(text) * 4 + 128),
                                   self.opt.num_predict_cap),
            },
        }

        last_err = None
        last_text = ""
        for attempt in range(1, self.opt.max_retries + 1):
            if self._cancel.is_set():
                raise RuntimeError("已取消")
            try:
                data = self._post(payload)
                self.stats["requests"] += 1
                out = self._clean((data.get("response") or "").strip(), text)

                if self.opt.verify:
                    bad = check_immediate(text, out, target_lang=target_lang)
                    if bad:
                        last_err = bad
                        if out:
                            last_text = out
                        self.stats["retried"] += 1
                        if attempt < self.opt.max_retries:
                            time.sleep(0.4 * attempt)
                        continue
                return out
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.opt.max_retries:
                    time.sleep(1.5 * attempt)

        if allow_partial and last_text:
            self.stats["unverified"] += 1
            return last_text
        raise RuntimeError(f"翻译失败（已重试 {self.opt.max_retries} 次）: {last_err}")

    # ---------------- 批量 ----------------

    def _build_batch_prompt(self, items: list, glossary: Glossary,
                            target_lang: str,
                            prior: Optional[list] = None) -> str:
        """items: [(idx, text, context)]

        ``prior`` 是本批之前的若干条原文（只读上文）。分块是按提取顺序切的，
        所以一批基本是**同一段连续对话的前后几句** —— 把上文交给模型，
        它才能消解代词与称呼（"它"/"那家伙"到底指谁）。
        """
        if self.prompt_style == "hymt":
            head = HYMT_BATCH_PROMPT.format(target_lang=target_lang)
        else:
            head = BATCH_PROMPT.replace("简体中文", target_lang)
        lines = [head]

        terms: dict = {}
        for _i, text, _c in items:
            terms.update(glossary.relevant_for(text))
        if terms:
            # 术语过多会挤占上下文，按长度降序取前 30 条
            picked = sorted(terms.items(), key=lambda kv: -len(kv[0]))[:30]
            sep = "翻译成" if self.prompt_style == "hymt" else "=>"
            lines.append("术语表：" + "；".join(f"{k}{sep}{v}" for k, v in picked))

        # ---- 上下文注入（v2.5）----
        # 翻译专用模型（Hy-MT 等）走极简模板，多加结构反而干扰输出，跳过。
        if self.opt.inject_context and self.prompt_style != "hymt":
            cast = [(i, (c or {}).get("speaker")) for i, _t, c in items]
            cast = [(i, s) for i, s in cast if s]
            if cast:
                lines.append(
                    "本批是同一段连续对话。各条说话人（仅供把握语气与称呼，"
                    "不要出现在译文里）："
                    + "；".join(f"{i}={s}" for i, s in cast))
            if prior:
                ov = max(1, int(self.opt.context_overlap))
                shown = prior[-ov:]
                lines.append(
                    f"上文（前 {len(shown)} 条，仅供理解衔接，"
                    "不要翻译、不要出现在输出里）：")
                for j, t in enumerate(shown, 1):
                    lines.append(f"  [{j}] {t}")

        lines.append("")
        lines.append("输入：")
        lines.append(json.dumps({str(i): t for i, t, _c in items},
                                ensure_ascii=False))
        lines.append("")
        lines.append("输出：")
        return "\n".join(lines)

    def translate_multi(self, items: list, glossary: Optional[Glossary] = None,
                        target_lang: str = "简体中文",
                        prior: Optional[list] = None,
                        ) -> tuple[dict, Optional[str]]:
        """一次请求翻译多条。

        Args:
            items: [(idx, text, context)]
            prior: 本批之前的若干条原文，作只读上文（见 ``_build_batch_prompt``）

        Returns:
            (结果字典 {idx: 译文}, 失败原因)。失败时结果为空，由调用方降级处理。
        """
        glossary = glossary or Glossary()
        prompt = self._build_batch_prompt(items, glossary, target_lang,
                                          prior=prior)
        total_chars = sum(len(t) for _i, t, _c in items)

        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": "",                      # 约束已写在 prompt 里，省一次前缀
            "stream": False,
            "format": "json",                  # 强制 JSON，便于对齐
            "keep_alive": self.opt.keep_alive,
            "options": {
                "temperature": self.temperature,
                "top_p": 0.9,
                "num_predict": min(max(256, total_chars * 4 + 200),
                                   self.opt.num_predict_cap),
            },
        }

        try:
            data = self._post(payload)
            self.stats["requests"] += 1
            raw = (data.get("response") or "").strip()
        except Exception as e:  # noqa: BLE001
            return {}, f"请求失败: {e}"

        parsed = self._parse_json_map(raw)
        if parsed is None:
            return {}, f"返回内容不是合法 JSON: {raw[:80]!r}"

        # 键集合必须与输入一致，否则宁可整批作废也不能错位
        want = {str(i) for i, _t, _c in items}
        got = set(parsed.keys())
        if got != want:
            return {}, f"编号不匹配（期望 {len(want)} 个，返回 {len(got)} 个）"

        out: dict = {}
        for i, text, _c in items:
            val = parsed.get(str(i))
            if not isinstance(val, str):
                return {}, f"第 {i} 条不是字符串"
            cleaned = self._clean(val.strip(), text)
            leak = looks_leaked(cleaned, text)
            if leak:
                # 整批作废比"猜哪一段是译文"安全 —— 失败条目会在续传时重试。
                return {}, f"第 {i} 条{leak}"
            if self.opt.verify:
                bad = check_immediate(text, cleaned, target_lang=target_lang)
                if bad:
                    return {}, f"第 {i} 条校验不过：{bad}"
            out[i] = cleaned
        return out, None

    @staticmethod
    def _parse_json_map(raw: str) -> Optional[dict]:
        """从模型输出里抠出 JSON 对象。容忍代码块包裹和前后噪声。"""
        if not raw:
            return None
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.startswith("json"):
                s = s[4:]
            s = s.strip()
        for cand in (s, ):
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except Exception:  # noqa: BLE001
                pass
        # 退一步：截取第一个 { 到最后一个 }
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                obj = json.loads(s[i:j + 1])
                if isinstance(obj, dict):
                    return obj
            except Exception:  # noqa: BLE001
                pass
        return None

    # ---------------- 输出清理 ----------------

    @staticmethod
    def _clean(out: str, original: str) -> str:
        """清理模型输出：去掉常见的前缀、引号、说明。"""
        s = (out or "").strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.startswith("json") or s.startswith("text"):
                s = s.split("\n", 1)[-1]
        for prefix in ("译文：", "译文:", "翻译：", "翻译:", "中文：", "中文:"):
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”":
            s = s[1:-1]
        # ★★ 换行是**内容**，不是噪声（2026-10-08 CQ2 换行丢失事故换来的）。
        #
        # 早期实现在这里写了 ``s.replace("\n", " ")`` —— 本意是"模型爱换行，
        # 压成一行更整齐"。实测后果：CQ2 的 `stringtable/*` 里，服装说明这类
        # 多行文本原版写成引号内的 ``\r\n`` 转义（如 ``Text="Focus: Attack\r\n
        # Type: Melee\r\n..."``，游戏靠它分行显示）。译文回到这里时 ``\r`` 被
        # 保留、``\n`` 被压成空格 → 落盘成 ``"\r "``，**整段文本挤成一行**
        # （DRCI051/054/057、COST003/005/009/012/015/018/021/027/030 等 18 条）。
        #
        # 现在一律保留换行，只做 CRLF/CR → LF 归一，避免同一条里混着三种换行。
        s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
        return s

    # ---------------- 批量流水线 ----------------

    def translate_batch(
        self,
        units: list,
        glossary: Optional[Glossary] = None,
        target_lang: str = "简体中文",
        progress: Optional[Callable[[int, int, str], None]] = None,
        save_every: int = 20,
        on_save: Optional[Callable[[], None]] = None,
        skip_translated: bool = True,
        options: Optional[TranslateOptions] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> int:
        """批量翻译 TextUnit 列表，实时回写 translated 字段。

        流程：预过滤 → 去重 → 分块 → 并发 → 校验 → 分发。

        Args:
            units: TextUnit 列表（原地修改 translated）
            on_save: 每 save_every 条调用一次。入参是**本批新增**的译文
                     ``[(uid, 译文, 是否未通过校验), ...]``，调用方据此做增量
                     追加即可，不需要（也不应该）重写整个工程文件
            skip_translated: 跳过已有译文的条目
            options: 覆盖实例级的并发/批量配置
            cancel_check: 返回 True 则尽快中止

        Returns:
            本次实际写入译文的条目数（含去重分发出去的）
        """
        opt = options or self.opt
        self.opt = opt
        glossary = glossary or Glossary()
        self._cancel = threading.Event()

        # ---- 1. 预过滤 + 去重 ----
        todo = [u for u in units if not (skip_translated and u.translated)]
        groups: dict = {}          # 保护后原文 -> [unit, ...]
        skipped = 0

        for u in todo:
            text = u.extra.get("protected_form") or u.original
            if opt.skip_useless and should_skip(text, target_lang):
                # 无需翻译：原样写回，避免回填时留下空洞
                if not u.translated:
                    u.translated = u.original
                skipped += 1
                continue
            groups.setdefault(text, []).append(u)

        self.stats["skipped"] += skipped
        keys = list(groups.keys())
        self.stats["unique"] += len(keys)
        total_units = len(todo)
        total_unique = len(keys)

        if not keys:
            if on_save:
                on_save([])          # 入参约定是「本批新增的译文列表」
            return skipped

        # ---- 2. 分块 ----
        # 短文本批量是亏的 —— JSON 结构开销（约 8 token/条）比译文本身还长，
        # 生成量翻几倍，实测反而比单条更慢。所以只在平均原文够长
        # （接近对话长度、prefill 开销占比高）时才启用批量。
        avg_len = sum(len(k) for k in keys) / max(len(keys), 1)
        bs = max(1, opt.batch_size) if opt.use_batch else 1
        if bs > 1 and avg_len < opt.batch_min_len:
            self.stats["batch_off"] = 1
            bs = 1
        # 每批附带前 N 条原文当「只读上文」（v2.5）。分块本身已按提取顺序切，
        # 所以"上一批的尾部几条"就是天然的前文 —— 纯白捡的上下文。
        ov = max(0, int(opt.context_overlap)) if opt.inject_context else 0
        chunks: list = []
        for _s in range(0, len(keys), bs):
            _body = keys[_s:_s + bs]
            # 单条批走 translate_one，没有 prior 参数，就不带了
            _prior = keys[max(0, _s - ov):_s] if (ov and len(_body) > 1) else []
            chunks.append((_prior, _body))

        # ---- 3. 并发执行 ----
        lock = threading.Lock()
        finished_units = [0]          # 已产出的 unit 数（含跳过）
        pending: list = []            # 待落盘的译文 [(uid, 译文, 是否未通过校验)]

        def cancelled() -> bool:
            if self._cancel.is_set():
                return True
            return bool(cancel_check and cancel_check())

        def store(key: str, text: str, unverified: bool = False) -> None:
            """把结果分发回该原文对应的所有 unit。"""
            with lock:
                for u in groups[key]:
                    u.translated = text
                    if unverified:
                        u.extra["unverified"] = True
                    else:
                        u.extra.pop("unverified", None)
                    pending.append((u.uid, text, 1 if unverified else 0))
                finished_units[0] += len(groups[key])

        def flush() -> None:
            """把新增译文交给 on_save 落盘。

            取出与清空都在锁内完成 —— 否则 worker 线程可能在 `list(pending)`
            与 `pending.clear()` 之间追加，导致这批译文永远丢在内存里。
            """
            if on_save is None:
                return
            with lock:
                if not pending:
                    return
                batch = list(pending)
                pending.clear()
            try:
                on_save(batch)
            except Exception:  # noqa: BLE001
                pass

        def work(prior: list, chunk: list) -> None:
            if cancelled():
                return
            # --- 批量尝试 ---
            if len(chunk) > 1:
                items = [(i, k, groups[k][0].context)
                         for i, k in enumerate(chunk, 1)]
                res, err = self.translate_multi(items, glossary, target_lang,
                                                prior=prior)
                if not err:
                    self.stats["batched"] += 1
                    if prior:
                        self.stats["ctx"] += 1
                    for i, k in enumerate(chunk, 1):
                        store(k, res[i])
                    return
                # 批量失败 -> 降级为逐条（不放弃这批）
                self.stats["degraded"] += 1

            # --- 逐条（也可能是批量降级后的兜底）---
            for k in chunk:
                if cancelled():
                    return
                try:
                    u = groups[k][0]
                    out = self.translate_one(k, glossary, u.context, target_lang,
                                             allow_partial=True)
                    # 兜底保留的译文要自己判定是否真的过校验 —— 通过了就是正常
                    # 译文，没通过才标 unverified（会让工程照常走到 100%，但留下
                    # 一份「值得人工扫一眼」的清单，而不是永远留空反复重翻）
                    uv = bool(check_immediate(k, out, target_lang=target_lang))
                    store(k, out, unverified=uv)
                except Exception:  # noqa: BLE001
                    self.stats["failed"] += 1
                    continue

        workers = max(1, min(opt.concurrency, len(chunks)))
        last_save = [0]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, c[0], c[1]) for c in chunks]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    pass

                with lock:
                    done = finished_units[0]
                if progress:
                    progress(min(done + skipped, total_units), total_units,
                             f"{done} 条已译 / 去重后 {total_unique} 个唯一串")

                if on_save and done - last_save[0] >= save_every:
                    last_save[0] = done
                    flush()

                if cancelled():
                    break

        flush()

        return skipped + finished_units[0]

    # ---------------- 统计 ----------------

    def stats_line(self) -> str:
        s = self.stats
        parts = []
        if s["skipped"]:
            parts.append(f"跳过无需翻译 {s['skipped']} 条")
        if s["unique"]:
            parts.append(f"去重后 {s['unique']} 个唯一串")
        if s["batched"]:
            parts.append(f"批量请求 {s['batched']} 次")
        if s.get("ctx"):
            parts.append(f"其中带上下文 {s['ctx']} 批")
        if s["degraded"]:
            parts.append(f"降级逐条 {s['degraded']} 批")
        if s.get("batch_off"):
            parts.append("文本偏短，已自动关闭批量")
        if s["retried"]:
            parts.append(f"校验重试 {s['retried']} 次")
        if s.get("unverified"):
            parts.append(f"兜底保留 {s['unverified']} 条（未过硬校验，建议校对）")
        if s["failed"]:
            parts.append(f"失败 {s['failed']} 条")
        return " · ".join(parts) if parts else "无特殊事件"
