# -*- coding: utf-8 -*-
"""第三层：模型抽检（选做，成本较高）。

和第二层「规则校对」的区别：

    第二层能抓的是**机械性问题**（空译文、日文残留、术语没命中、长度异常），
    纯靠正则和统计，秒级、免费，但看不出「翻得对不对」。

    第三层让模型读「原文 + 译文」，判断有没有漏译、误译、语义不通。
    这才是真正的语义质检。

成本控制的关键在于**让模型只输出问题编号**：

    一次送 20 条「原文+译文」（约 500 token），模型只回一个 ``[2,5]``。
    输出只有几个 token，所以生成成本极低，瓶颈在输入侧。
    7.7 万条抽 10% = 7700 条，按 20 条一批只需约 385 次请求。

支持两种调用方式：
- 按比例抽样（默认 10%）—— 用于整体质量摸底；
- 只复检指定条目（``only_uids``）—— 用于复核第二层标出来的可疑项。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.validate import Issue

REVIEW_PROMPT = """你是游戏本地化的质检员。下面每组给出「原文」和「译文」。

请找出译文**明显有问题**的组，判定标准只有这几条：
- 漏译：原文有内容，译文没体现
- 误译：意思翻错、说反了
- 语义不通：读起来不知所云、前后矛盾
- 机翻腔严重：不符合中文说话习惯，读着别扭

注意：人名、术语的译法差异**不算问题**；轻微的润色差异**不算问题**。
宁可少报，也不要滥报。

只输出有问题的编号数组，例如 [2,5,9]。
若全部合格，输出 []。
不要输出任何解释文字。

"""


@dataclass
class ReviewResult:
    checked: int = 0
    batches: int = 0
    failed_batches: int = 0
    flagged: list = field(default_factory=list)   # list[Issue]

    def summary(self) -> str:
        if not self.checked:
            return "深度校对：没有可检查的条目。"
        if not self.flagged:
            return (f"深度校对：抽查 {self.checked} 条，"
                    f"模型未发现问题（{self.batches} 批）。")
        return (f"深度校对：抽查 {self.checked} 条，"
                f"模型判定 {len(self.flagged)} 条存疑"
                f"（{self.batches} 批"
                + (f"，{self.failed_batches} 批请求失败" if self.failed_batches else "")
                + "）。")


def sample_deterministic(units: list, rate: float) -> list:
    """按固定步长抽样 —— 同样的输入永远抽到同样的样本，便于复现对比。"""
    if rate >= 1.0 or not units:
        return list(units)
    n = max(1, int(len(units) * rate))
    if n >= len(units):
        return list(units)
    step = len(units) / n
    return [units[int(i * step)] for i in range(n)]


class QualityChecker:
    """用翻译模型自身做质检。"""

    def __init__(self, translator, concurrency: int = 4):
        """
        Args:
            translator: OllamaTranslator 实例（复用它的 HTTP 通道与模型配置）
            concurrency: 并发批次数
        """
        self.tr = translator
        self.concurrency = max(1, concurrency)

    # ---------------- 内部 ----------------

    @staticmethod
    def _build_prompt(items: list) -> str:
        """items: [(idx, original, translated)]"""
        lines = [REVIEW_PROMPT]
        for i, src, dst in items:
            lines.append(f"{i}. 原文：{src}")
            lines.append(f"   译文：{dst}")
        lines.append("")
        lines.append("输出：")
        return "\n".join(lines)

    @staticmethod
    def _parse_flags(raw: str) -> Optional[list]:
        """从模型输出里解析出编号数组。兼容 []、[1,2] 和 {"problems":[1,2]}。"""
        if not raw:
            return None
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.startswith("json"):
                s = s[4:]
            s = s.strip()

        def _extract(obj):
            if isinstance(obj, list):
                return [int(x) for x in obj
                        if isinstance(x, (int, float, str)) and str(x).strip().isdigit()]
            if isinstance(obj, dict):
                # 常见包装：{"problems": [...]} / {"issues": [...]}
                for v in obj.values():
                    if isinstance(v, list):
                        return _extract(v)
                # 也可能返回 {"1": true, "5": false}
                out = []
                for k, v in obj.items():
                    if v in (True, "true", "yes", 1) and str(k).strip().isdigit():
                        out.append(int(k))
                return out
            return None

        try:
            return _extract(json.loads(s))
        except Exception:  # noqa: BLE001
            pass
        i, j = s.find("["), s.rfind("]")
        if 0 <= i < j:
            try:
                return _extract(json.loads(s[i:j + 1]))
            except Exception:  # noqa: BLE001
                pass
        return None

    def _review_batch(self, items: list) -> tuple[list, bool]:
        """items: [(idx, original, translated)] -> (问题编号列表, 是否成功)"""
        prompt = self._build_prompt(items)
        payload = {
            "model": self.tr.model,
            "prompt": prompt,
            "system": "",
            "stream": False,
            "format": "json",
            "keep_alive": self.tr.opt.keep_alive,
            "options": {
                "temperature": 0.0,        # 质检要稳定，不能有创造性
                "top_p": 0.9,
                "num_predict": 128,        # 只需要输出几个编号
            },
        }
        try:
            data = self.tr._post(payload)
            self.tr.stats["requests"] += 1
            raw = (data.get("response") or "").strip()
        except Exception:  # noqa: BLE001
            return [], False
        flags = self._parse_flags(raw)
        if flags is None:
            return [], False
        return flags, True

    # ---------------- 对外 ----------------

    def review(
        self,
        units: list,
        sample_rate: float = 0.1,
        per_batch: int = 20,
        only_uids: Optional[set] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> ReviewResult:
        """对已翻译条目做语义抽检。

        Args:
            units: 全部条目（只会检查已有译文的）
            sample_rate: 抽样比例，仅当未指定 only_uids 时生效
            per_batch: 每批送多少条给模型
            only_uids: 只检查这些 uid（用于复核第二层的结果）
            progress: (当前批, 总批数, 描述)
            cancel_check: 返回 True 则中止

        Returns:
            ReviewResult
        """
        res = ReviewResult()

        pool = [u for u in units if u.translated and u.translated.strip()]
        if only_uids is not None:
            pool = [u for u in pool if u.uid in only_uids]
        else:
            pool = sample_deterministic(pool, sample_rate)

        if not pool:
            return res

        res.checked = len(pool)
        batches = [pool[i:i + per_batch]
                   for i in range(0, len(pool), per_batch)]

        def work(batch: list) -> tuple:
            """返回 (问题编号列表 或 None 表示本批失败, 该批条目)。"""
            if cancel_check and cancel_check():
                return None, batch
            items = [(i, (u.extra.get("protected_form") or u.original),
                      u.translated.strip())
                     for i, u in enumerate(batch, 1)]
            flags, ok = self._review_batch(items)
            if not ok:
                return None, batch
            return flags, batch

        done = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futures = [ex.submit(work, b) for b in batches]
            for fut in as_completed(futures):
                try:
                    flags, batch = fut.result()
                except Exception:  # noqa: BLE001
                    flags, batch = None, []
                res.batches += 1
                if flags is None:
                    # 本批请求失败（网络/解析），不计入结果也不误报为「合格」
                    res.failed_batches += 1
                    continue
                for idx in flags:
                    if not (1 <= idx <= len(batch)):
                        continue
                    u = batch[idx - 1]
                    res.flagged.append(Issue(
                        u.uid, "model_flag",
                        "模型判定译文存疑（漏译/误译/不通顺）",
                        u.original, u.translated or "", u.source_file))
                done += 1
                if progress:
                    progress(done, len(batches), f"已检查 {done * per_batch} 条")

                if cancel_check and cancel_check():
                    break

        return res
