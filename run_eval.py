#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评估脚本：把 eval_dataset.json 里的问题依次交给 Agent 跑一遍，
统计三个指标，并生成 eval_report.md（这就是 JD 里说的「评估数据集 + 评估报告」）。

指标定义（面试一定要能说清楚）：
  · 工具选择正确率 = 该调工具的问题里，模型确实调了预期工具的比例；不该调工具的问题没有乱调
  · 关键点命中率   = 最终回答中包含预期关键信息的比例（一条样本列了多个关键词时，
                     命中任意一个就算通过，这样对措辞差异更宽容）
  · 平均耗时/步数  = 反映成本和效率

用法：
  python run_eval.py            # 调用真实模型
  python run_eval.py --mock     # 离线模式，只验证流程（不联网、不花钱）
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from agent import load_config, run_agent

BASE_DIR = Path(__file__).resolve().parent
DATASET_FILE = BASE_DIR / "eval_dataset.json"
REPORT_FILE = BASE_DIR / "eval_report.md"
RAW_FILE = BASE_DIR / "eval_results.json"


def tool_ok(expected, called):
    """expected 可以是工具名、工具名列表，或 "none"（表示不该调用任何工具）。"""
    if expected == "none" or expected is None:
        return len(called) == 0
    if isinstance(expected, list):
        return any(name in called for name in expected)
    return expected in called


def main():
    mock = "--mock" in sys.argv
    cfg = load_config()
    if not mock and not cfg["api_key"]:
        print("没有配置模型密钥，无法跑真实评估。想先验证流程请加 --mock。")
        return 1

    items = json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    rows = []
    print("开始评估，共 %d 条样本%s" % (len(items), "（离线演示模式）" if mock else "（调用真实模型）"))

    for index, item in enumerate(items, 1):
        started = time.time()
        try:
            result = run_agent(item["question"], cfg, mock=mock, verbose=False)
            answer = result.get("answer", "")
            called = [c["name"] for c in result.get("tools_called", [])]
            steps = result.get("steps", 0)
            error = result.get("error") or ""
        except Exception as exc:
            answer, called, steps, error = "", [], 0, str(exc)
        elapsed = time.time() - started

        ok_tool = tool_ok(item.get("expected_tool"), called)
        keywords = item.get("expected_keywords") or []
        hit = [k for k in keywords if k in answer]
        ok_keyword = (not keywords) or bool(hit)

        rows.append({
            "id": item.get("id", index),
            "question": item["question"],
            "point": item.get("point", ""),
            "expected_tool": item.get("expected_tool"),
            "called_tools": called,
            "ok_tool": ok_tool,
            "expected_keywords": keywords,
            "hit_keywords": hit,
            "ok_keyword": ok_keyword,
            "steps": steps,
            "elapsed": round(elapsed, 2),
            "answer": answer,
            "error": error,
        })
        print("[%2d/%d] %-34s 工具:%-4s 关键点:%-4s %4.1fs" % (
            index, len(items), item["question"][:32],
            "通过" if ok_tool else "未过", "通过" if ok_keyword else "未过", elapsed))

    total = len(rows)
    tool_pass = sum(1 for r in rows if r["ok_tool"])
    keyword_pass = sum(1 for r in rows if r["ok_keyword"])
    errors = [r for r in rows if r["error"]]
    avg_time = sum(r["elapsed"] for r in rows) / total if total else 0
    avg_steps = sum(r["steps"] for r in rows) / total if total else 0

    lines = []
    lines.append("# Agent 评估报告")
    lines.append("")
    lines.append("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("- 评估模式：%s" % ("离线演示模式（--mock，不调用真实模型，结果仅用于验证流程）"
                                     if mock else "真实模型：%s（%s）" % (cfg["model"], cfg["base_url"])))
    lines.append("- 样本数量：%d 条" % total)
    lines.append("- 数据集文件：`eval_dataset.json`")
    lines.append("")
    lines.append("## 一、总体指标")
    lines.append("")
    lines.append("| 指标 | 结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append("| 工具选择正确率 | %d/%d = %.0f%% | 该调工具时调对，不该调时不乱调 |"
                 % (tool_pass, total, 100.0 * tool_pass / total if total else 0))
    lines.append("| 关键点命中率 | %d/%d = %.0f%% | 最终回答里含预期关键信息（多条关键词命中任意一条即通过） |"
                 % (keyword_pass, total, 100.0 * keyword_pass / total if total else 0))
    lines.append("| 平均耗时 | %.2f 秒 | 反映响应速度 |" % avg_time)
    lines.append("| 平均工具调用步数 | %.2f 步 | 反映任务复杂度与成本 |" % avg_steps)
    lines.append("| 出错条数 | %d | 接口异常或超出最大步数 |" % len(errors))
    lines.append("")
    lines.append("## 二、逐条明细")
    lines.append("")
    lines.append("| # | 问题 | 考点 | 预期工具 | 实际调用 | 工具 | 关键点 | 步数 | 耗时(s) |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        expected = r["expected_tool"]
        if isinstance(expected, list):
            expected = "/".join(expected)
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %d | %.1f |" % (
            r["id"], r["question"], r["point"], expected,
            "/".join(r["called_tools"]) or "无",
            "通过" if r["ok_tool"] else "未通过",
            "通过" if r["ok_keyword"] else "未通过",
            r["steps"], r["elapsed"]))
    lines.append("")
    lines.append("## 三、失败样本与改进方向")
    lines.append("")
    failed = [r for r in rows if not (r["ok_tool"] and r["ok_keyword"])]
    if not failed:
        lines.append("本次全部通过。")
    else:
        for r in failed:
            lines.append("- **%s**" % r["question"])
            lines.append("  - 实际调用：%s；回答：%s" % ("/".join(r["called_tools"]) or "无",
                                                        (r["answer"] or "").replace("\n", " ")[:120]))
            lines.append("  - 可能原因：工具描述不够清楚 / 提示词规则不够明确 / 关键信息没被工具返回")
    lines.append("")
    lines.append("## 四、下一步怎么改（把这段话讲给面试官听）")
    lines.append("")
    lines.append("1. 先看工具选择失败的那几条：多数是工具的 description 写得不够具体，模型不知道该用哪个。")
    lines.append("2. 再看关键点失败的那几条：把要求写进 system prompt（例如「回答必须给出具体数字」）。")
    lines.append("3. 每次改完重跑这个脚本，用指标对比改动效果，而不是靠感觉。")
    lines.append("")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    RAW_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n评估报告已生成：%s" % REPORT_FILE)
    print("工具选择正确率 %.0f%%，关键点命中率 %.0f%%，平均耗时 %.2f 秒"
          % (100.0 * tool_pass / total if total else 0,
             100.0 * keyword_pass / total if total else 0, avg_time))
    return 0


if __name__ == "__main__":
    sys.exit(main())
