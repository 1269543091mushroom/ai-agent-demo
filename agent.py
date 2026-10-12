#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
岗位小助手 —— 一个最小的 AI Agent 演示（只用 Python 标准库，不需要装任何第三方包）

这个程序把「大模型」和「你本地的数据 / 计算能力」连起来：
模型自己决定什么时候查数据、什么时候算数，而不是靠它凭印象编。

三层结构（面试时可以照着讲）：
  1) 模型层  call_llm()    —— 和任意 OpenAI 兼容接口对话（DeepSeek / 通义千问 / 智谱 / Kimi ...）
  2) 工具层  search_jobs() / salary_stats() / calculate()  —— 真正干活的本地函数
  3) 编排层  run_agent()   —— 手写的 Agent 循环：模型决策 -> 执行工具 -> 结果回传 -> 再决策

运行方式：
  python agent.py "合肥有哪些工艺工程师岗位？"      # 问一个问题
  python agent.py                                    # 进入交互模式，可以一直问
  python agent.py --mock "合肥有哪些岗位？"          # 离线演示模式（不联网、不调用模型）
"""

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 可选依赖：装了 jsonschema 就用来严格校验模型给的参数；没装也能跑（本项目零依赖也能运行）
try:
    import jsonschema
except ImportError:
    jsonschema = None

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "jobs.csv"
CONFIG_FILE = BASE_DIR / "config.txt"

# Agent 最多循环几步。防止模型反复调工具停不下来（工程上一定要有这个保险）
MAX_STEPS = 6

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


# ---------------------------------------------------------------------------
# 一、配置：优先读环境变量，其次读同目录下的 config.txt
# ---------------------------------------------------------------------------
def load_config():
    cfg = {
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "base_url": os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).strip(),
        "model": os.environ.get("LLM_MODEL", DEFAULT_MODEL).strip(),
    }
    if CONFIG_FILE.exists():
        for raw_line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in cfg and value:
                cfg[key] = value
    return cfg


# ---------------------------------------------------------------------------
# 二、数据层：岗位数据（演示用的虚构数据，放在 data/jobs.csv）
# ---------------------------------------------------------------------------
_JOBS = None


def load_jobs():
    global _JOBS
    if _JOBS is None:
        if not DATA_FILE.exists():
            raise FileNotFoundError("找不到数据文件：%s" % DATA_FILE)
        with open(DATA_FILE, newline="", encoding="utf-8-sig") as handle:
            _JOBS = list(csv.DictReader(handle))
    return _JOBS


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _brief(job):
    return "%s | %s | %s | %s | %s | %s-%s元/月 | 技能：%s" % (
        job.get("job_title", ""),
        job.get("city", ""),
        job.get("department", ""),
        job.get("degree", ""),
        job.get("experience", ""),
        job.get("salary_min", ""),
        job.get("salary_max", ""),
        job.get("skills", ""),
    )


# ---------------------------------------------------------------------------
# 三、工具层：模型可以调用的三个本地函数
# ---------------------------------------------------------------------------
def search_jobs(keyword="", city="", degree=""):
    """按关键词 / 城市 / 学历筛选岗位。关键词会匹配岗位名称、部门、技能、专业。"""
    rows = load_jobs()
    kw = str(keyword or "").strip().lower()
    ct = str(city or "").strip()
    dg = str(degree or "").strip()
    hits = []
    for job in rows:
        blob = " ".join([
            job.get("job_title", ""),
            job.get("department", ""),
            job.get("skills", ""),
            job.get("major", ""),
        ]).lower()
        if kw and kw not in blob:
            continue
        if ct and ct not in job.get("city", ""):
            continue
        if dg and dg not in job.get("degree", ""):
            continue
        hits.append(job)
    result = {"筛选条件": {"关键词": kw, "城市": ct, "学历": dg}, "匹配数量": len(hits),
              "岗位": [_brief(j) for j in hits[:5]]}
    if len(hits) > 5:
        result["说明"] = "共 %d 条，这里只返回前 5 条" % len(hits)
    if not hits:
        result["提示"] = "没有匹配的岗位，建议放宽条件（换个关键词或去掉城市/学历限制）"
    return json.dumps(result, ensure_ascii=False)


def salary_stats(city=""):
    """统计岗位数量和薪资水平：岗位数、平均月薪、最高/最低、薪资前三的岗位。"""
    rows = load_jobs()
    ct = str(city or "").strip()
    selected = [job for job in rows if (not ct or ct in job.get("city", ""))]
    if not selected:
        return json.dumps({"错误": "没有找到城市：%s" % ct}, ensure_ascii=False)
    averages = [(_to_int(job["salary_min"]) + _to_int(job["salary_max"])) / 2.0 for job in selected]
    ranked = sorted(zip(averages, selected), key=lambda pair: pair[0], reverse=True)
    result = {
        "范围": ct or "全部城市",
        "岗位数": len(selected),
        "平均月薪": round(sum(averages) / len(averages)),
        "最低月薪(下限)": min(_to_int(job["salary_min"]) for job in selected),
        "最高月薪(上限)": max(_to_int(job["salary_max"]) for job in selected),
        "薪资前三岗位": ["%s %.0f元/月" % (job.get("job_title", ""), avg) for avg, job in ranked[:3]],
    }
    return json.dumps(result, ensure_ascii=False)


_SAFE_EXPR = re.compile(r"^[0-9+\-*/(). ]+$")


def calculate(expression):
    """安全计算器：只允许数字和 + - * / ( ) . 空格，避免把用户输入当代码执行。"""
    expr = str(expression or "").strip()
    if not _SAFE_EXPR.match(expr):
        return json.dumps({"错误": "表达式只允许数字和 + - * / ( ) . 空格，收到：%s" % expr},
                          ensure_ascii=False)
    try:
        value = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 —— 已用白名单正则限制输入
    except Exception as exc:
        return json.dumps({"错误": "计算失败：%s" % exc}, ensure_ascii=False)
    return json.dumps({"表达式": expr, "结果": value}, ensure_ascii=False)


# 给模型看的「工具说明书」：这就是工具调用的关键——模型只负责选工具和给参数
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": "按关键词、城市、学历查询在招岗位明细。当用户想看具体有哪些岗位、"
                           "某个岗位的要求、用技能或专业反查岗位时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "关键词，如 工艺工程师、设备、大模型、材料"},
                    "city": {"type": "string", "description": "城市，如 合肥、上海"},
                    "degree": {"type": "string", "description": "学历要求，如 本科、硕士"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "salary_stats",
            "description": "统计某个城市或全部城市的岗位数量与薪资水平：岗位数、平均月薪、"
                           "最高/最低月薪、薪资前三的岗位。当用户问数量、平均薪资、哪个城市更高、"
                           "薪资排名时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市，留空表示全部城市"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "做四则运算。算薪资差额、年收入、平均值等数字时必须调用它，不要在脑子里算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string",
                                   "description": "数学表达式，只能用数字和 + - * / ( )，例如 (12000*14)"},
                },
                "required": ["expression"],
            },
        },
    },
]

TOOL_FUNCS = {
    "search_jobs": search_jobs,
    "salary_stats": salary_stats,
    "calculate": calculate,
}


# 每个工具允许的参数名 / 参数 schema，用来清洗和校验模型给出的参数
TOOL_PARAM_KEYS = {
    spec["function"]["name"]: set((spec["function"]["parameters"].get("properties") or {}).keys())
    for spec in TOOL_SPECS
}
TOOL_PARAM_SCHEMAS = {spec["function"]["name"]: spec["function"]["parameters"] for spec in TOOL_SPECS}


def clean_args(name, args):
    """清洗模型给的参数：只保留工具定义里存在的字段，值统一转成字符串。
    这样模型偶尔多给字段、给了 null、或者把数字写成字符串，都不会让工具直接崩掉。
    装了 jsonschema 时再严格校验一次，校验不过就把原因回传给模型，让它自己改。
    返回 (清洗后的参数, 错误说明或 None)。
    """
    allowed = TOOL_PARAM_KEYS.get(name)
    cleaned = {}
    if allowed:
        for key, value in args.items():
            if key not in allowed or value is None or isinstance(value, (dict, list)):
                continue
            cleaned[key] = value if isinstance(value, str) else str(value)
    else:
        cleaned = dict(args)

    if jsonschema is None:
        return cleaned, None
    schema = TOOL_PARAM_SCHEMAS.get(name)
    if not schema:
        return cleaned, None
    try:
        jsonschema.validate(instance=cleaned, schema=schema)
    except jsonschema.ValidationError as exc:
        return cleaned, "参数不符合工具要求：%s" % exc.message
    return cleaned, None


# ---------------------------------------------------------------------------
# 四、角色设定（system prompt）：这部分就是 JD 里说的「角色设定 / 提示词工程」
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是「半导体行业求职小助手」，服务对象是准备投递半导体、存储行业岗位的学生。

你的工作规则：
1. 先判断问题要不要查数据。涉及具体岗位、薪资、数量、计算时，必须调用工具，不许凭印象编。
2. 工具返回的结果是唯一事实来源。回答里出现的岗位、薪资、数字，必须来自工具结果或对工具结果的计算。
3. 需要多个信息时可以连续调用多次工具（比如先分别查两个城市，再算差额）。
4. 工具没查到就如实说明，并给出放宽条件的建议，不要编造岗位。
5. 用中文回答，简洁清楚，先说结论再给依据；列岗位时用短清单，每条一行。
6. 如果用户问数据来源、或问这些岗位是否真实，如实说明：
   本地岗位库是项目自带的演示数据，不代表真实招聘信息。

如果用户只是打招呼或问你的能力范围，直接回答，不要调用工具。"""


# ---------------------------------------------------------------------------
# 五、模型层：调用 OpenAI 兼容接口
# ---------------------------------------------------------------------------
def call_llm(messages, cfg, timeout=60):
    """把对话历史发给模型，返回 {'content': 文本, 'tool_calls': [工具调用]}。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0}
    payload["tools"] = TOOL_SPECS
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
    }
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError("接口返回错误 %s：%s" % (exc.code, detail))
    except Exception as exc:
        raise RuntimeError("调用模型失败（请检查网络、base_url、api_key）：%s" % exc)

    message = body["choices"][0]["message"]
    calls = []
    for item in message.get("tool_calls") or []:
        function = item.get("function") or {}
        calls.append({
            "id": item.get("id", ""),
            "name": function.get("name", ""),
            "arguments": function.get("arguments") or "{}",
        })
    return {"content": message.get("content") or "", "tool_calls": calls}


# ---------------------------------------------------------------------------
# 六、离线演示模式：不联网的假模型（用规则模拟"模型决定调用哪个工具"）
#     用途：没网的时候也能演示流程；也方便你在没有密钥时先跑通代码。
# ---------------------------------------------------------------------------
CITY_WORDS = ["合肥", "上海", "北京", "无锡", "西安", "深圳"]

KEYWORD_WORDS = [
    "工艺工程师", "设备工程师", "良率工程师", "测试工程师", "版图设计工程师", "数字IC验证工程师",
    "模拟IC设计工程师", "封装工程师", "可靠性工程师", "数据分析工程师", "AI算法工程师",
    "工艺整合工程师", "MES工程师", "硬件工程师", "失效分析工程师", "生产计划工程师",
    "EHS工程师", "数据平台开发工程师", "人力资源专员",
    "大模型", "提示词", "RAG", "智能体", "Python", "SQL", "IT",
    "材料", "微电子", "失效分析", "版图", "硬件", "数据", "工艺", "设备", "测试",
]


def _tool_call(name, arguments):
    return {
        "content": "",
        "tool_calls": [{"id": "mock-1", "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False)}],
    }


def mock_llm(messages):
    """假模型：先看有没有工具结果，有就直接总结；没有就用关键词规则决定调哪个工具。"""
    tool_results = [m.get("content", "") for m in messages if m.get("role") == "tool"]
    question = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            question = message.get("content", "")
            break

    if tool_results:
        return {"content": "根据工具返回的数据：" + "；".join(tool_results), "tool_calls": []}

    q = question
    # 打招呼/问能力范围：不该调用任何工具（对应评估集第 20 条）
    if any(k in q for k in ("你好", "你是谁", "你能帮我做什么", "你能做什么", "介绍一下", "自我介绍")):
        return {
            "content": "你好，我是半导体行业求职小助手。我可以帮你查岗位信息、统计薪资水平、"
                       "算薪资差额和年收入。比如你可以问：合肥有哪些工艺工程师岗位？",
            "tool_calls": [],
        }

    # 把「15万」这类中文数量词换算成数字，方便取数
    normalized = q.replace("万", "0000")
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", normalized)]
    city = next((c for c in CITY_WORDS if c in q), "")
    keyword = next((k for k in KEYWORD_WORDS if k in q), "")
    degree = "硕士" if "硕士" in q else ("本科" if "本科" in q else "")

    # 计算类
    if any(k in q for k in ("年收入", "年薪", "乘以", "除以", "差多少", "平均是多少", "算一下", "帮我算")):
        expr = None
        if ("年收入" in q or "年薪" in q or "14薪" in q) and numbers:
            expr = "%g*%d" % (numbers[0], 14 if "14" in q else 12)
        elif "除以" in q and len(numbers) >= 2:
            expr = "%g/%g" % (numbers[0], numbers[1])
        elif "平均" in q and numbers:
            expr = "(" + "+".join("%g" % n for n in numbers) + ")/%d" % len(numbers)
        elif "差" in q and len(numbers) >= 2:
            expr = "%g-%g" % (numbers[0], numbers[1])
        if expr:
            return _tool_call("calculate", {"expression": expr})

    # 有明确岗位/技能关键词 -> 查岗位明细
    if keyword:
        args = {"keyword": keyword}
        if city:
            args["city"] = city
        if degree:
            args["degree"] = degree
        return _tool_call("search_jobs", args)

    # 数量、平均、排名类 -> 统计
    if any(k in q for k in ("多少", "几个", "数量", "平均", "最高", "排名", "哪个城市", "更高", "统计")):
        return _tool_call("salary_stats", {"city": city} if city else {})

    # 其他情况统一去查岗位
    args = {}
    if city:
        args["city"] = city
    if degree:
        args["degree"] = degree
    return _tool_call("search_jobs", args)


# ---------------------------------------------------------------------------
# 七、编排层：Agent 循环
# ---------------------------------------------------------------------------
def run_agent(question, cfg=None, mock=False, verbose=True):
    cfg = cfg or load_config()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tools_called = []
    steps = 0

    for step in range(1, MAX_STEPS + 1):
        steps = step
        reply = mock_llm(messages) if mock else call_llm(messages, cfg)
        calls = reply.get("tool_calls") or []

        # 模型不再调工具 -> 说明它准备好了最终答案
        if not calls:
            answer = (reply.get("content") or "").strip()
            if verbose:
                print("\n[最终回答]\n" + answer)
            return {"answer": answer, "tools_called": tools_called, "steps": steps, "error": None}

        messages.append({
            "role": "assistant",
            "content": reply.get("content") or "",
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]}}
                           for c in calls],
        })

        for call in calls:
            name = call["name"]
            raw_args = (call.get("arguments") or "{}").strip()
            try:
                args = json.loads(raw_args) if raw_args else {}
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}

            func = TOOL_FUNCS.get(name)
            args, arg_error = clean_args(name, args)
            if func is None:
                output = json.dumps({"错误": "没有这个工具：%s" % name}, ensure_ascii=False)
            elif arg_error:
                output = json.dumps({"错误": arg_error}, ensure_ascii=False)
            else:
                try:
                    output = func(**args)
                except TypeError as exc:
                    output = json.dumps({"错误": "参数不对：%s" % exc}, ensure_ascii=False)
                except Exception as exc:
                    output = json.dumps({"错误": "工具执行失败：%s" % exc}, ensure_ascii=False)

            tools_called.append({"name": name, "arguments": args})
            if verbose:
                print("  [第%d步] 调用工具 %s(%s)" % (step, name, json.dumps(args, ensure_ascii=False)))
                print("           返回：%s" % output[:220])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})

    return {"answer": "（达到最大步骤数 %d，未能给出最终回答）" % MAX_STEPS,
            "tools_called": tools_called, "steps": steps, "error": "max_steps"}


# ---------------------------------------------------------------------------
# 八、命令行入口
# ---------------------------------------------------------------------------
def main():
    # 让输出立刻显示出来：万一输出被接进了管道，默认会攒够一批才显示，看起来就像"卡住没反应"
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("=== 岗位小助手 启动 ===")
    print("Python 版本：" + sys.version.split()[0])
    print("收到的参数：" + repr(sys.argv[1:]))
    try:
        print("数据文件：" + DATA_FILE.name + "，共 %d 条岗位" % len(load_jobs()))
    except Exception as exc:
        print("[警告] 读取数据文件失败：%s" % exc)
    print()

    argv = [a for a in sys.argv[1:] if a.strip()]
    mock = "--mock" in argv
    argv = [a for a in argv if a != "--mock"]
    cfg = load_config()

    if mock:
        print("离线演示模式（--mock）：不调用真实大模型，用规则模拟工具调用。")
    else:
        if not cfg["api_key"]:
            print("还没有配置模型密钥。")
            print("请把 config.example.txt 复制成 config.txt，填上你的 api_key 后再运行。")
            print("想先离线看看效果，可以加参数 --mock。")
            return 1
        print("模型：%s   |   接口：%s" % (cfg["model"], cfg["base_url"]))

    question = " ".join(argv).strip()
    if question:
        print("用户提问：" + question)
        try:
            run_agent(question, cfg, mock=mock, verbose=True)
        except RuntimeError as exc:
            print("[出错] " + str(exc))
            return 1
        return 0

    print("输入问题后回车（直接回车退出）。例如：合肥有哪些工艺工程师岗位？")
    while True:
        try:
            text = input("\n你问：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            return 0
        try:
            run_agent(text, cfg, mock=mock, verbose=True)
        except RuntimeError as exc:
            print("[出错] " + str(exc))


if __name__ == "__main__":
    sys.exit(main())
