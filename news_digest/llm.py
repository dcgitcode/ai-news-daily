"""极简 LLM 客户端：把英文论文改写成中文速读。

默认走硅基流动（OpenAI 兼容接口），模型用其免费档（THUDM/GLM-4-9B-0414）。
设计原则：
- 未配置 LLM_API_KEY，或调用失败，一律返回空串/空元组，由调用方降级为「只发标题+链接」，
  绝不让中文速读失败拖垮整条速递。
- 密钥只放在 Authorization 头里、不进 URL，因此打印状态码不会泄漏密钥。
"""

import os
import re

import requests

DEFAULT_BASE_URL = 'https://api.siliconflow.cn/v1'
DEFAULT_MODEL = 'THUDM/GLM-4-9B-0414'

DIGEST_PROMPT = """你是中文科技编辑，把英文学术论文改写成中文速读，读者是中国的高职院校教师。

输出格式（严格遵守）：
第一行：中文标题，不超过 30 字，专业术语保留英文原名（如 VLA、Diffusion Policy、MuJoCo）
第二行起：2 到 3 句中文速读，依次说清「解决什么问题」「方法关键点」「结果或意义」

不要编号，不要 Markdown，不要任何解释性开场白，全文不超过 150 字。

英文标题：{title}
英文摘要：{abstract}"""

# 模型偶尔会加上「中文标题：」「速读：」之类的引导词，统一剥掉。
LABEL = re.compile(r'^\s*(?:中文标题|标题|速读|摘要|要点)\s*[:：]\s*')
# 只剥 Markdown 记号与「1. 」式编号：不能误伤「7000亿参数」这类以数字开头的正常标题。
HEADING = re.compile(r'^\s*(?:[#*>\-]+\s*|\d+\s*[\.、）)]\s*)+')


def enabled():
    return bool(os.getenv('LLM_API_KEY', '').strip())


def chat(messages, model=None, temperature=0.3, max_tokens=600, timeout=(10, 60)):
    """调用 OpenAI 兼容的 chat/completions，失败返回空串。"""
    key = os.getenv('LLM_API_KEY', '').strip()
    if not key:
        return ''
    # 注意：Actions 里未配置的 secret 会展开成空字符串，所以必须「取到空就当没配」，
    # 不能用 os.getenv('LLM_BASE_URL', 默认值)——那样会拿到 '' 拼出残缺 URL。
    base = os.getenv('LLM_BASE_URL', '').strip() or DEFAULT_BASE_URL
    chosen = model or os.getenv('LLM_MODEL', '').strip() or DEFAULT_MODEL
    payload = {'model': chosen, 'messages': messages,
               'temperature': temperature, 'max_tokens': max_tokens}
    try:
        response = requests.post(base + '/chat/completions', json=payload, timeout=timeout,
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json'})
    except Exception as exc:
        print(f'WARNING llm: {type(exc).__name__}')
        return ''
    if response.status_code != 200:
        # 状态码安全（密钥在请求头里，不在 URL 上）；响应体可能含敏感信息，不打。
        print(f'WARNING llm: HTTP {response.status_code}')
        return ''
    try:
        content = response.json()['choices'][0]['message']['content'] or ''
    except (KeyError, IndexError, TypeError, ValueError):
        print('WARNING llm: unexpected response shape')
        return ''
    return content.strip()


def digest_zh(title, abstract):
    """把单篇英文论文改写成 (中文标题, 中文速读)；失败返回 ('', '')。"""
    text = chat([{'role': 'user',
                  'content': DIGEST_PROMPT.format(title=title, abstract=abstract[:1600])}])
    if not text:
        return '', ''
    lines = [LABEL.sub('', line).strip() for line in text.splitlines()]
    lines = [HEADING.sub('', line) for line in lines if line.strip()]
    if not lines:
        return '', ''
    return lines[0][:40], ' '.join(lines[1:])[:180]
