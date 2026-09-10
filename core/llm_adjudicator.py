from __future__ import annotations
"""
LLM 裁决器 (L1) — 对管线产出的人工审核行做"带证据"的大模型裁决

设计原则:
    1. LLM 只做小候选集内的裁决, 不重做召回 — 候选全部来自发现层输出,
       幻觉风险被结构性抑制(LLM 无法凭空捏造上游条目);
    2. 证据注入 — 把已算好的通道分数、锚点命中、微块验证着色统计一并
       喂给 LLM, 它是"带证据评审"而非"裸评";
    3. 结构化输出 + 分级处置 — 高置信自动确认(解除存疑)、中置信附意见、
       低置信/冲突仍归人工, 人工出口永不关闭;
    4. 降级保证 — LLM 未启用或调用失败时, 行为与无此模块完全一致。

触发范围(按组, 即同一下游单元):
    - 存疑行(ambiguous): 算法无法区分 top1/top2, 交 LLM 裁决
    - 未溯源行(untraced): top3 参考候选中是否有真命中(按需跑微块验证)
    - hybrid 补漏行([added]): 确认补漏关系是否成立
    - hybrid 纠偏行([suspicious]): 表中关系 vs 发现建议二选一仲裁

调用协议: OpenAI 兼容 REST(/chat/completions), requests 直连零新依赖;
    换供应商只需改 TRACE_NL_LLM_BASE_URL / TRACE_NL_LLM_MODEL。
"""
import hashlib
import json
import os
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

warnings.filterwarnings('ignore')  # 环境 requests 版本告警抑制

import config
from config import (
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
    LLM_TIMEOUT, LLM_MAX_WORKERS, LLM_MAX_RETRY, LLM_TEMPERATURE,
    LLM_AUTO_CONFIRM, LLM_OPINION_MIN, LLM_CACHE_DIR,
    LLM_DS_MAX_CHARS, LLM_CAND_MAX_CHARS, LLM_MAX_TOKENS, LLM_MAX_TOKENS_CAP,
    LLM_UNTRACED_CANDS, LLM_DEFAULT_CANDS,
)

# ============================================================
# 数据结构
# ============================================================


@dataclass
class AdjudicationTask:
    """一个待裁决组的任务"""
    task_type: str                  # 'ambiguous' | 'untraced' | 'added' | 'suspicious'
    ds_id: str                      # 下游条目号
    ds_content: str                 # 下游内容
    candidates: list[dict] = field(default_factory=list)
    # 每个候选: {'label','doc','key','content','score','evidence','verify_stat'}
    # label 为 "候选N"(untraced/ambiguous/added) 或 "表中关系"/"发现建议"(suspicious)
    note: str = ''                  # 附加背景说明
    fail_reason: str = ''           # LLM调用失败原因(透传前端诊断)


@dataclass
class LLMVerdict:
    """一次裁决的结构化结果"""
    verdict: str          # 候选label 或 "无合适候选"
    confidence: float     # 0~1
    key_evidence: str     # 最关键的原文证据片段
    reasoning: str        # 裁决理由
    cached: bool = False  # 是否命中缓存


# ============================================================
# Prompt 构建
# ============================================================

_SYSTEM_PROMPT = """你是核仪控工程(NPP I&C)需求追踪评审专家。给你一条下游需求内容与若干上游候选单元, 判断下游内容真正承接/细化自哪个候选。

判定规则:
1. 只依据内容承接关系判断: 技术术语、性能指标、安全分级、条款细化方向、功能覆盖关系;
2. 条目编号或章节号数字的巧合不构成任何证据(如 SyRS002 与 F-SC2 的数字相似没有意义);
3. 追踪方向是"下游细化上游": 下游通常把上游一个条款展开为更具体的多个要求, 下游只覆盖上游的一个侧面是常态;
4. 安全分级代码(如F-SC1、F-SC2)是"对象标识"而非内容特征: 下游声明了某一分级时, 不同分级的候选即使内容几乎逐字相同(模板近似), 也只是不同对象的要求, 不构成正确溯源;
5. 若所有候选均无充分的内容承接依据, 如实回答"无合适候选";
6. 管线给出的通道证据与微块匹配统计仅供参考, 最终以你对内容的判断为准。

输出硬性要求(违反即失败):
- 严禁输出任何解释、推理过程、分析、计划、前言、总结或 markdown 代码块围栏;
- 你的完整响应必须以 { 开头并以 } 结尾, 只包含一个 JSON 对象;
- JSON 字段: verdict(候选N 或 无合适候选), confidence(0到1的小数), key_evidence(字符串), reasoning(字符串)。"""

_TASK_TYPE_DESC = {
    'ambiguous': '存疑配对裁决 — 算法无法区分前两名候选(融合分差距<0.05), 请你裁决谁为正确溯源',
    'untraced': '未溯源判断 — 算法未找到达标的溯源关系, 请判断参考候选中是否有真命中',
    'added': '补漏关系确认 — 追踪表中缺失该条目的关系, 发现层补入了此候选, 请确认补漏是否成立',
    'suspicious': '表与发现不一致仲裁 — 追踪表登记的关系与发现层建议不同, 请判断哪个正确',
}


def _clip(text: str, limit: int) -> str:
    """内容截断(保留超长标记)"""
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f'…(截断, 原文{len(text)}字)'


def build_user_prompt(task: AdjudicationTask) -> str:
    """构建用户消息: 下游内容 + 分级声明 + 各候选 + 管线证据"""
    # 惰性导入避免模型链路被提前加载(与代码库其他位置一致)
    from core.candidate_discovery import extract_class_codes

    parts = [
        f'【裁决类型】{_TASK_TYPE_DESC.get(task.task_type, task.task_type)}',
        '',
        f'【下游条目】{task.ds_id}',
        _clip(task.ds_content, LLM_DS_MAX_CHARS),
    ]
    ds_cls = extract_class_codes(task.ds_content)
    if ds_cls:
        parts.append(f'(下游声明安全分级: {"、".join(sorted(ds_cls))})')
    if task.note:
        parts.append('')
        parts.append(f'【背景】{task.note}')
    for cand in task.candidates:
        parts.append('')
        head = f'【{cand["label"]}】{cand["doc"]} / {cand["key"]}'
        if cand.get('score'):
            head += f' (算法融合分 {cand["score"]:.2f})'
        parts.append(head)
        parts.append(_clip(cand.get('content', ''), LLM_CAND_MAX_CHARS))
        c_cls = extract_class_codes(cand.get('key', '')) | \
            extract_class_codes(cand.get('content', ''))
        if c_cls:
            parts.append(f'(候选声明安全分级: {"、".join(sorted(c_cls))})')
        ev = cand.get('evidence') or ''
        if ev:
            parts.append(f'管线证据: {ev}')
        vs = cand.get('verify_stat') or ''
        if vs:
            parts.append(f'微块匹配: {vs}')
    return '\n'.join(parts)


# ============================================================
# LLM 调用 (OpenAI 兼容 REST + 磁盘缓存 + 重试降级)
# ============================================================

def _cache_key(system: str, user: str) -> str:
    raw = json.dumps(
        {'model': LLM_MODEL, 'system': system, 'user': user},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(LLM_CACHE_DIR, f'{key}.json')


def _load_cache(key: str) -> dict | None:
    path = _cache_path(key)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _save_cache(key: str, data: dict) -> None:
    try:
        os.makedirs(LLM_CACHE_DIR, exist_ok=True)
        with open(_cache_path(key), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # 缓存写失败不影响主流程


def _extract_json(text: str) -> dict | None:
    """从模型输出中解析 JSON(容错: 剥代码围栏/前后杂文字)"""
    if not text:
        return None
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _session_id() -> str:
    """稳定会话ID(供 x-opencode-session 请求头, 便于OpenCode Go提示词缓存)"""
    import config as _cfg
    raw = _cfg.PROJECT_ROOT + '|' + LLM_MODEL + '|' + LLM_BASE_URL
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


_REPAIR_PROMPT = ('你上一次的输出是无效的——它包含解释文字或不是完整 JSON, 系统判定失败。'
                  '现在请严格只输出一个 JSON 对象, 禁止任何解释、推理、前言或代码块围栏, '
                  '响应必须以 { 开头并以 } 结尾。'
                  '字段: verdict(候选N或"无合适候选"), confidence(0~1数字), '
                  'key_evidence(字符串), reasoning(字符串)。')

# 线程局部: 记录最近一次调用失败原因(并发场景各线程独立, 供adjudicate透传)
_llm_err = threading.local()


def _is_transient(exc: Exception) -> bool:
    """是否为值得重试的瞬时错误(超时/连接中断/限流/5xx)。

    鉴权失败(401/403)、模型不存在(404)等非瞬时错误不重试。
    """
    import requests as _rq
    if isinstance(exc, (_rq.exceptions.Timeout, _rq.exceptions.ConnectionError)):
        return True
    if isinstance(exc, _rq.exceptions.HTTPError):
        status = getattr(getattr(exc, 'response', None), 'status_code', 0) or 0
        return status in (429, 500, 502, 503, 504)
    return False


def _call_llm(system: str, user: str) -> dict | None:
    """调用 OpenAI 兼容 /chat/completions, 返回解析后的 JSON dict

    鲁棒性策略:
    1. 首次用 json_object 模式; 端点返回 400(不支持) 时退普通模式;
    2. 解析失败时把模型原样输出回喂(assistant)并追加"只输出JSON"的
       纠正指令(user) — 覆盖推理型模型把思考过程当正文输出的场景;
    3. 瞬时网络错误(超时/连接中断/429/5xx)指数退避重试, 其余异常不重试。
    """
    import requests

    url = LLM_BASE_URL.rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {LLM_API_KEY}',
               'Content-Type': 'application/json',
               'User-Agent': config.LLM_USER_AGENT,
               'x-opencode-session': _session_id()}

    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]

    last_err = None
    max_attempts = LLM_MAX_RETRY + 2   # 含截断扩容轮与格式纠错轮
    max_tokens = LLM_MAX_TOKENS
    for attempt in range(max_attempts):
        with_json_mode = (attempt == 0)  # 仅首轮尝试 json_object
        try:
            payload = {
                'model': LLM_MODEL,
                'messages': messages,
                'temperature': LLM_TEMPERATURE,
                'max_tokens': max_tokens,
            }
            if with_json_mode:
                payload['response_format'] = {'type': 'json_object'}
            resp = requests.post(url, headers=headers,
                                 json=payload, timeout=LLM_TIMEOUT)
            if resp.status_code == 400 and with_json_mode:
                # 端点不支持 json_object, 立即以普通模式重试
                payload.pop('response_format', None)
                resp = requests.post(url, headers=headers,
                                     json=payload, timeout=LLM_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get('choices') or [{}])[0]
            msg = choice.get('message', {}) or {}
            finish_reason = choice.get('finish_reason') or ''
            content = msg.get('content') or ''
            if not content.strip():
                # 部分推理型模型把正文放在 reasoning_content 字段
                content = msg.get('reasoning_content') or ''
            parsed = _extract_json(content)
            if parsed is not None:
                return parsed
            if finish_reason == 'length' or not content.strip():
                # 截断: 思考占满输出配额, JSON未写出。
                # 回喂纠错无意义(内容本身被切断), 倍增上限原样重试。
                last_err = (f'输出被截断(finish_reason={finish_reason or "empty"}, '
                            f'len={len(content)}, max_tokens={max_tokens})')
                if max_tokens >= LLM_MAX_TOKENS_CAP:
                    break  # 已到上限仍截断, 放弃
                max_tokens = min(max_tokens * 2, LLM_MAX_TOKENS_CAP)
                continue
            last_err = (f'无法解析模型输出(尝试{attempt + 1}, http={resp.status_code}, '
                        f'json_mode={with_json_mode}, finish={finish_reason}, '
                        f'len={len(content)}): {str(content)[:160]!r}')
            # 格式错误: 把模型原样输出回喂(assistant)并追加纠正指令
            messages.append({'role': 'assistant', 'content': content[:800] or '(空)'})
            messages.append({'role': 'user', 'content': _REPAIR_PROMPT})
        except Exception as e:  # noqa: BLE001 — 任何调用失败都降级
            last_err = str(e)[:200]
            # 瞬时错误(超时/连接中断/限流/5xx)退避重试;
            # 非瞬时错误(鉴权/参数等)重试无意义, 直接放弃。
            if not _is_transient(e) or attempt >= max_attempts - 1:
                break
            time.sleep(min(2 ** attempt, 8))
    if last_err:
        _llm_err.value = last_err
        print(f'    [llm] 调用失败(已降级跳过): {last_err}')
    return None


def adjudicate(task: AdjudicationTask) -> LLMVerdict | None:
    """单个裁决任务: 查缓存 → 调LLM → 规范化 LLMVerdict"""
    if not config.LLM_ENABLED:
        task.fail_reason = '未启用 LLM(未配置 TRACE_NL_LLM_API_KEY)'
        return None
    user = build_user_prompt(task)
    key = _cache_key(_SYSTEM_PROMPT, user)

    cached = _load_cache(key)
    if cached is not None:
        obj = cached
        is_cached = True
    else:
        obj = _call_llm(_SYSTEM_PROMPT, user)
        if obj is None:
            task.fail_reason = getattr(_llm_err, 'value', 'LLM调用失败(原因未知)')
            return None
        _save_cache(key, obj)
        is_cached = False

    # verdict 规范化: 匹配到候选label, 匹配不到视为"无合适候选"
    verdict = str(obj.get('verdict', '')).strip()
    labels = [c['label'] for c in task.candidates]
    if verdict not in labels and '无' not in verdict:
        # 尝试模糊匹配(模型可能输出"候选1: xxx"形态)
        for lb in labels:
            if lb in verdict:
                verdict = lb
                break
        else:
            verdict = '无合适候选'
    try:
        conf = float(obj.get('confidence', 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(max(conf, 0.0), 1.0)

    return LLMVerdict(
        verdict=verdict,
        confidence=conf,
        key_evidence=str(obj.get('key_evidence', ''))[:120],
        reasoning=str(obj.get('reasoning', ''))[:200],
        cached=is_cached,
    )


# ============================================================
# 矩阵级入口: 筛选待裁决组 → 并发裁决 → 写回
# ============================================================

def _verify_stat(match_result) -> str:
    """由微块验证着色结果生成统计摘要(绿/蓝/黑字符占比)"""
    if not match_result:
        return ''
    runs = match_result.downstream_runs or []
    if not runs:
        return ''
    g = b = k = 0
    for run in runs:
        n = len(run.text)
        cat = getattr(run.category, 'value', run.category)
        if cat == 'exact':
            g += n
        elif cat == 'semantic':
            b += n
        else:
            k += n
    total = g + b + k
    if total == 0:
        return ''
    overall = getattr(match_result.overall_category, 'value', '')
    desc = {'exact': '整体一致', 'semantic': '整体部分匹配'}.get(overall, '整体不一致')
    return f'绿{g / total:.0%} 蓝{b / total:.0%} 黑{k / total:.0%} ({desc})'


def _build_task(rows: list) -> AdjudicationTask | None:
    """由一个合并组的行构建裁决任务; 无需裁决时返回 None"""
    first = rows[0]
    cands_meta = first.discovery_candidates or []
    if not cands_meta:
        return None  # table来源或无发现层元数据

    untraced = not any(r.upstream_ref for r in rows)
    added = any((r.evidence or '').startswith('[added]') for r in rows)
    suspicious = any('[suspicious]' in (r.evidence or '') for r in rows)
    ambiguous = any(r.ambiguous for r in rows)
    if not (untraced or added or suspicious or ambiguous):
        return None  # 高置信普通行不裁决

    if suspicious:
        # 仲裁双方: 表中关系(行上) vs 发现建议(top候选)
        candidates = [{
            'label': '表中关系',
            'doc': first.upstream_doc,
            'key': first.upstream_ref,
            'content': first.upstream_content,
            'score': 0.0,
            'evidence': '',
            'verify_stat': _verify_stat(first.match_result),
        }]
        for i, c in enumerate(cands_meta[:2]):
            candidates.append({
                'label': f'发现建议{i + 1}',
                'doc': c.get('doc', ''),
                'key': c.get('key', ''),
                'content': c.get('content', ''),
                'score': c.get('score', 0.0),
                'evidence': c.get('evidence', ''),
                'verify_stat': c.get('verify_stat', ''),
            })
        task_type = 'suspicious'
        note = '追踪表登记的关系与内容发现层的建议不一致, 请判断哪个是正确的溯源'
    else:
        task_type = 'untraced' if untraced else ('added' if added else 'ambiguous')
        # 候选数: 未溯源组扩大审查面(算法全部不达标恰说明排序不可靠,
        # 正确答案可能排在第4~5名); 其余类型维持3名(争点就在前两名)
        n_cands = LLM_UNTRACED_CANDS if untraced else LLM_DEFAULT_CANDS
        # 组内已有关系(主/次级)有微块验证; 组外参考候选按需补跑微块验证
        in_group = {(r.upstream_doc, r.upstream_ref) for r in rows if r.upstream_ref}
        candidates = []
        for i, c in enumerate(cands_meta[:n_cands]):
            doc, key = c.get('doc', ''), c.get('key', '')
            vstat = ''
            if (doc, key) in in_group:
                for r in rows:
                    if r.upstream_doc == doc and r.upstream_ref == key \
                            and r.match_result:
                        vstat = _verify_stat(r.match_result)
                        break
            elif untraced:
                vstat = _verify_stat(_quick_verify(first.downstream_content,
                                                    c.get('content', '')))
            candidates.append({
                'label': f'候选{i + 1}',
                'doc': doc, 'key': key,
                'content': c.get('content', ''),
                'score': c.get('score', 0.0),
                'evidence': c.get('evidence', ''),
                'verify_stat': vstat,
            })
        note = ''
        if untraced:
            note = '算法未找到达标的溯源关系(最高融合分低于接受阈值), 以下为参考候选'

    return AdjudicationTask(
        task_type=task_type,
        ds_id=first.downstream_id,
        ds_content=first.downstream_content,
        candidates=candidates,
        note=note,
    )


def _quick_verify(ds_content: str, up_content: str) -> object:
    """对参考候选按需跑一次微块匹配(单对, 便宜)"""
    try:
        from core.text_matcher import match_text_pair
        return match_text_pair(ds_content, up_content)
    except Exception:  # noqa: BLE001 — 验证失败不影响裁决
        return None


def _resolve_candidate_refs(task: AdjudicationTask, text: str) -> str:
    """
    把裁决文本中的"候选N"替换为真实候选标识(文档:键)。

    LLM输出常引用"候选2明确提及…"——最终报告里没有候选清单上下文,
    直接展示会让审查者不知"候选2"是谁。此处将引用解析为可读标识。
    """
    if not text:
        return text
    import re as _re
    label_map = {}
    for c in task.candidates:
        short = f'{c["doc"]}:{str(c["key"])[:16]}'
        label_map[c['label']] = short
    # 按label长度降序替换(避免"候选1"先匹配吃掉"候选10"前缀)
    for label in sorted(label_map, key=len, reverse=True):
        text = text.replace(label, f'「{label_map[label]}」')
    return text


def _apply_verdict(matrix, indices: list[int],
                   task: AdjudicationTask, verdict: LLMVerdict) -> str:
    """把裁决写回组内行, 返回处置摘要"""
    conf = verdict.confidence
    # 低置信(<0.6)不产生任何处置: 维持现状仅记录, 人工照常审核
    if conf < LLM_OPINION_MIN:
        return f'{task.task_type}→低置信跳过(置信{conf:.2f})'
    key_ev = _resolve_candidate_refs(task, verdict.key_evidence)
    reason = _resolve_candidate_refs(task, verdict.reasoning)
    ev = f'证据: {key_ev}' if key_ev else ''
    rs = f'理由: {reason}' if reason else ''
    tail = '; '.join(x for x in (ev, rs) if x)
    conf_s = f'置信{conf:.2f}'
    rows = [matrix.rows[i] for i in indices]
    first = rows[0]

    cand_label = verdict.verdict
    cand_desc = ''
    for c in task.candidates:
        if c['label'] == cand_label:
            cand_desc = f'{c["doc"]}:{c["key"]}'
            break

    if task.task_type == 'suspicious':
        if cand_label == '表中关系' and conf >= LLM_OPINION_MIN:
            opinion = f'AI支持表中关系 ({conf_s}). {tail}'
        elif cand_label.startswith('发现建议') and conf >= LLM_OPINION_MIN:
            opinion = f'AI支持发现建议 {cand_desc} ({conf_s}). {tail}'
        else:
            opinion = f'AI无法可靠仲裁 ({conf_s}). {tail}'
        for r in rows:
            r.ai_opinion = opinion
        return f'suspicious→{cand_label}'

    if cand_label == '无合适候选':
        if conf >= LLM_AUTO_CONFIRM:
            opinion = (f'AI认同{"未溯源" if task.task_type == "untraced" else "均不成立"} '
                       f'({conf_s}). {tail}')
            for r in rows:
                r.ai_opinion = opinion
            return f'{task.task_type}→AI认同无'
        if conf >= LLM_OPINION_MIN:
            opinion = f'AI倾向无合适候选 ({conf_s}). {tail}'
            for r in rows:
                r.ai_opinion = opinion
            return f'{task.task_type}→AI倾向无'
        return f'{task.task_type}→低置信跳过'

    # 裁决指向某个具体候选
    # 该候选是否命中组内已有任一关系行(主/次级皆可)。
    # 对存疑组而言 AI 的任务就是在组内候选间做选择, 高置信选中任一行
    # 都应解除存疑, 而不是只认"第一行"(它可能并非金标)。
    in_group_refs = {(r.upstream_doc, r.upstream_ref) for r in rows if r.upstream_ref}
    is_current = any(
        c['label'] == cand_label and (c['doc'], c['key']) in in_group_refs
        for c in task.candidates
    )
    # 裁决选中的引用(组内该行=AI首选; 其余行为非首选, 如分级不符的模板近似次级)
    sel_ref = next(((c['doc'], c['key']) for c in task.candidates
                    if c['label'] == cand_label), None)

    if is_current:
        if conf >= LLM_AUTO_CONFIRM:
            for r in rows:  # 解除存疑标记 — 人工审核量直接下降的核心动作
                r.ambiguous = False
                if sel_ref and (r.upstream_doc, r.upstream_ref) == sel_ref:
                    r.ai_opinion = f'AI确认溯源关系 {cand_desc} ({conf_s}). {tail}'
                else:
                    own = f'{r.upstream_doc}:{r.upstream_ref}'
                    r.ai_opinion = (f'AI建议复核: 本行追溯至 {own}, '
                                    f'非AI首选(首选={cand_desc}, {conf_s}). {tail}')
            return f'{task.task_type}→AI确认并解除存疑'
        for r in rows:
            if sel_ref and (r.upstream_doc, r.upstream_ref) == sel_ref:
                r.ai_opinion = f'AI倾向所选关系 {cand_desc} ({conf_s}). {tail}'
            else:
                own = f'{r.upstream_doc}:{r.upstream_ref}'
                r.ai_opinion = (f'AI建议复核: 本行追溯至 {own}, '
                                f'AI倾向 {cand_desc} ({conf_s}). {tail}')
        return f'{task.task_type}→AI倾向所选(保留存疑)'

    # AI 支持的是非当前候选(或untraced下的参考候选) — 只给建议, 关系留给人工
    if conf >= LLM_AUTO_CONFIRM:
        opinion = (f'AI建议追溯至 {cand_desc} ({conf_s}), 请人工确认. {tail}')
    elif conf >= LLM_OPINION_MIN:
        opinion = f'AI弱倾向 {cand_desc} ({conf_s}). {tail}'
    else:
        opinion = f'AI无法判断 ({conf_s}). {tail}'
    for r in rows:
        r.ai_opinion = opinion
    return f'{task.task_type}→AI建议{cand_label}'


def adjudicate_matrix(matrix) -> dict:
    """
    矩阵级裁决入口(在 verify_matrix 之后调用, 着色统计可注入证据)。

    筛选待裁决组 → 并发调用 LLM → 写回 ai_opinion / 解除存疑。
    LLM 未启用或全部失败时矩阵保持原样。
    """
    stats = {'enabled': config.LLM_ENABLED, 'n_tasks': 0, 'n_resolved': 0,
             'n_confirmed': 0, 'n_failed': 0}
    if not config.LLM_ENABLED:
        print('    [llm] 未配置 TRACE_NL_LLM_API_KEY, 跳过 AI 裁决(行为与原版一致)')
        return stats

    # 按 seq 分组(同一下游单元)
    from collections import defaultdict
    groups: dict[int, list[int]] = defaultdict(list)
    for i, row in enumerate(matrix.rows):
        groups[row.seq_number].append(i)

    tasks: list[tuple[list[int], AdjudicationTask]] = []
    for seq in sorted(groups):
        indices = groups[seq]
        rows = [matrix.rows[i] for i in indices]
        task = _build_task(rows)
        if task is not None:
            tasks.append((indices, task))

    stats['n_tasks'] = len(tasks)
    if not tasks:
        print('    [llm] 无待人工审核行, 无需 AI 裁决')
        return stats

    print(f'    [llm] 待裁决 {len(tasks)} 组, 模型 {LLM_MODEL}, '
          f'并发 {LLM_MAX_WORKERS} …')
    results: list[tuple[list[int], AdjudicationTask, LLMVerdict | None]] = []
    with ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS) as ex:
        futs = {ex.submit(adjudicate, t): (idx, t) for idx, t in tasks}
        for fut in as_completed(futs):
            idx, t = futs[fut]
            try:
                results.append((idx, t, fut.result()))
            except Exception as e:  # noqa: BLE001 — 单组失败不影响其他组
                t.fail_reason = f'线程异常: {str(e)[:150]}'
                results.append((idx, t, None))

    for indices, task, verdict in results:
        if verdict is None:
            stats['n_failed'] += 1
            stats.setdefault('fail_reasons', []).append(
                f"{task.ds_id} [{task.task_type}]: {task.fail_reason or '未知原因'}")
            continue
        summary = _apply_verdict(matrix, indices, task, verdict)
        if '解除存疑' in summary or 'AI确认' in summary:
            stats['n_confirmed'] += 1
        elif '低置信' not in summary:
            stats['n_resolved'] += 1
        print(f'    [llm] {task.ds_id} [{task.task_type}] '
              f'{verdict.verdict} {verdict.confidence:.2f}'
              f'{"(缓存)" if verdict.cached else ""}')

    print(f'    [llm] 完成: 裁决{len(tasks)}组, '
          f'高置信确认{stats["n_confirmed"]}, '
          f'给出意见{stats["n_resolved"]}, 失败{stats["n_failed"]}')
    return stats
