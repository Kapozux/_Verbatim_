"""Verbatim 版的 llm.py：其余文件照抄 Daemon（见 README.md），只有这个换了。

跟原版的区别：
  · 不用 openai / dotenv / rich（Verbatim 的环境里没有）：requests 直连阿里云百炼的 OpenAI 兼容端点，
    key 用 Verbatim Settings 里那把 DashScope key，模型同名（deepseek-v4-pro / flash、kimi）。
  · 每次调用记进 Verbatim 的 usage.db（purpose = repo_read，ref = 代码库 id），Settings → Costs 看得到。
  · 阿里云有内容审查：读到的某段输出被拒收时，把最近那条命令输出换成一句说明再试，而不是整次崩掉。
  · 不流式、不转圈：无头跑，输出进日志。
接口跟原版一样：MODELS、query_lm(messages, isCompression, current_model) → (文字, token 数, 成功否)、compression。
"""
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.append(ROOT)                    # 放最后：Daemon 自己的 actions / llm 先找到
import config  # noqa: E402
import usage  # noqa: E402

MODELS = {
    "deepseek": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "kimi": "kimi-k2.6",
}
CENSORED = ("（这条命令的输出被模型服务的内容审查拒收了，看不到。别再读同一段，"
            "换一个文件或换个关键词继续。）")
_CENSOR_MARKS = ("data_inspection", "DataInspection", "inappropriate")


def compression(messages, original_task): # 压缩上下文（对message做处理

   messages.append({"role": "user", "content": "目前token amount已经超标,执行压缩程序。你目前现在需要根据我完整的上下文进行如下的信息保留。对于用户信息尽可能保留原始信息，对模型的输出（非代码部分）进行保留，以及对bash/python 的返回结果做大部分的压缩。最后你将三者按比例返回给我一个完整的新上下文结果。"})  # 进行压缩命令的输入
   lm_output,token_amount_temp,state_lm = query_lm(messages,True)
   new_messages = [{
        "role": "system",
        "content": "你是一个代码编写的agent, 你需要根据用户的初步指令去完成目标，并且根据你代码出现的错误进行自我修正知道完成用户目标。如果你需要跑一个指令（一次只有一个bash指令），请用以下方式包装： ```bash-action\n<command>\n```. 如果你认为任务已经完成，请运行exit 指令。 记住，只做符合任务目的的一切行动，不许经过用户同意后擅自查看，修改，创建新文件。输出规范：不许使用md的语法。当程序运行成功且输出符合预期时，必须立即运行 exit 命令，不要再做额外的验证或优化。"
    }]
   new_messages.append({"role": "assistant", "content": lm_output})
   new_messages.append({"role": "user", "content": "原始任务（请勿忘记）：" + original_task})
   return new_messages


def _censor_last(messages):
    """最近一条命令输出换成说明（原地改，后面的轮次都看不到它）。已经换过了还被拒 → 问题不在这，返回 False。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        if m.get("content") == CENSORED:
            return False
        m["content"] = CENSORED
        return True
    return False


def query_lm(messages,isCompression=False,current_model = "deepseek"):
    model = MODELS.get(current_model, MODELS["deepseek-pro"])
    key = config.dashscope_key()
    if not key:
        print("API调用失败: Verbatim 的 Settings 里没有 DashScope（阿里云百炼）key")
        return "", 0, False
    url = config.ALIYUN_COMPAT_BASE.rstrip("/") + "/chat/completions"
    for attempt in range(5):
        body = [{"role": m["role"], "content": m["content"]} for m in messages]   # 去掉 time 之类的私有字段
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=(15, 600),
                              json={"model": model, "messages": body})
        except requests.RequestException as e:
            print(f"API调用失败: {e}")
            time.sleep(min(2 ** attempt, 20))
            continue
        if r.status_code == 200:
            data = r.json()
            usage.record_openai(data, "aliyun", model, "repo_read", ref=os.environ.get("VERBATIM_REPO_ID"))
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not isCompression:
                print(text, flush=True)
            return text, (data.get("usage") or {}).get("total_tokens", 0), True
        err = r.text[:300]
        print(f"API调用失败: {r.status_code} {err}")
        if any(k in err for k in _CENSOR_MARKS):
            if not _censor_last(messages):
                return "", 0, False
            continue
        time.sleep(min(2 ** attempt, 20))
    return "", 0, False
