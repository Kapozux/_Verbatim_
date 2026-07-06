"""
DashScope transcription engine (阿里云百炼).
Uses Paraformer ASR via REST API for speech-to-text.
"""

import json
import os
import time

import requests as http_requests

from config import DASHSCOPE_API_KEY, DASHSCOPE_ASR_MODEL

BASE_URL = 'https://dashscope.aliyuncs.com/api/v1'


def transcribe_audio(filepath, progress_callback=None, diarization=False,
                     speaker_count=None):
    """
    Transcribe audio using DashScope Paraformer via REST API.

    Args:
        diarization: 开启说话人分离（声纹），返回的每段会带 'speaker' 字段。
        speaker_count: 已知说话人数量时传入作为提示，能显著改善聚类；
            不填（None）则让阿里云自动判断人数。

    Returns:
        List of segment dicts with keys: timestamp (str), text (str),
        以及开启 diarization 时的 speaker。
    """
    api_key = DASHSCOPE_API_KEY or os.environ.get('DASHSCOPE_API_KEY', '')
    if not api_key:
        raise RuntimeError(
            "DashScope API Key 未设置。请在 .env 文件中设置 DASHSCOPE_API_KEY。"
        )

    if progress_callback:
        progress_callback(5)

    file_url = _upload_file(filepath, api_key)

    if progress_callback:
        progress_callback(15)

    task_id = _submit_task(file_url, api_key, diarization=diarization,
                           speaker_count=speaker_count)

    if progress_callback:
        progress_callback(20)

    result = _poll_task(task_id, api_key, progress_callback)

    if progress_callback:
        progress_callback(90)

    segments = _parse_result(result)

    if progress_callback:
        progress_callback(100)

    return segments


def _upload_file(filepath, api_key):
    """Upload a local file to DashScope temporary OSS and return oss:// URL."""
    filename = os.path.basename(filepath)

    policy_resp = http_requests.get(
        f'{BASE_URL}/uploads',
        headers={'Authorization': f'Bearer {api_key}'},
        params={'action': 'getPolicy', 'model': DASHSCOPE_ASR_MODEL},
        timeout=30,
    )
    policy_resp.raise_for_status()
    policy = policy_resp.json().get('data', {})

    upload_host = policy.get('upload_host')
    upload_dir = policy.get('upload_dir')
    if not upload_host or not upload_dir:
        raise RuntimeError("DashScope 文件上传凭证获取失败")

    oss_key = f"{upload_dir}/{filename}"

    with open(filepath, 'rb') as f:
        files = {
            'OSSAccessKeyId': (None, policy['oss_access_key_id']),
            'Signature': (None, policy['signature']),
            'policy': (None, policy['policy']),
            'x-oss-object-acl': (None, policy['x_oss_object_acl']),
            'x-oss-forbid-overwrite': (None, policy['x_oss_forbid_overwrite']),
            'key': (None, oss_key),
            'success_action_status': (None, '200'),
            'file': (filename, f),
        }
        upload_resp = http_requests.post(upload_host, files=files, timeout=300)
        if upload_resp.status_code not in (200, 204):
            raise RuntimeError(
                f"文件上传到 OSS 失败: HTTP {upload_resp.status_code}"
            )

    return f"oss://{oss_key}"


def _submit_task(file_url, api_key, diarization=False, speaker_count=None):
    """Submit a transcription task via REST API and return task_id."""
    parameters = {
        'language_hints': ['zh', 'en'],
    }
    if diarization:
        # paraformer-v2 声纹说话人分离
        parameters['diarization_enabled'] = True
        # 传入已知人数作为提示，远场/多人场景能明显改善聚类；不填则自动判断
        if speaker_count and speaker_count > 0:
            parameters['speaker_count'] = int(speaker_count)

    resp = http_requests.post(
        f'{BASE_URL}/services/audio/asr/transcription',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'X-DashScope-Async': 'enable',
            'X-DashScope-OssResourceResolve': 'enable',
        },
        json={
            'model': DASHSCOPE_ASR_MODEL,
            'input': {
                'file_urls': [file_url],
            },
            'parameters': parameters,
        },
        timeout=30,
    )

    # 4xx 时阿里云会在响应体里给出真实的 code/message（如格式不支持、无音轨、时长超限），
    # 直接 raise_for_status() 会把这些吞掉只剩笼统的 "400 Bad Request"，这里手动带出来。
    if resp.status_code >= 400:
        code = message = ''
        try:
            err = resp.json()
            code = err.get('code', '')
            message = err.get('message', '')
        except ValueError:
            message = resp.text[:300]
        detail = ' '.join(p for p in (code, message) if p) or f'HTTP {resp.status_code}'
        raise RuntimeError(f"DashScope 任务提交被拒 (HTTP {resp.status_code}): {detail}")

    data = resp.json()

    task_id = data.get('output', {}).get('task_id')
    if not task_id:
        msg = data.get('message', json.dumps(data, ensure_ascii=False))
        raise RuntimeError(f"DashScope 转写任务提交失败: {msg}")

    return task_id


def _poll_task(task_id, api_key, progress_callback=None):
    """Poll transcription task via REST API until completion."""
    POLL_INTERVAL_SECONDS = 2
    # 1 小时上限，避免任务卡死时前端 / 后台线程永远挂着
    MAX_WAIT_SECONDS = 60 * 60
    TERMINAL_FAIL_STATUSES = {'FAILED', 'CANCELED', 'CANCELLED', 'UNKNOWN'}

    poll_count = 0
    waited_seconds = 0
    while True:
        try:
            resp = http_requests.get(
                f'{BASE_URL}/tasks/{task_id}',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'X-DashScope-OssResourceResolve': 'enable',
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except http_requests.RequestException as e:
            # 4xx（鉴权/参数/任务不存在）说明请求本身有问题，重试也不会好
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code is not None and 400 <= status_code < 500:
                raise RuntimeError(
                    f"DashScope 查询任务失败 (HTTP {status_code})，请检查 API Key 是否正确"
                ) from e
            # 5xx / 网络抖动：等下一轮，靠 MAX_WAIT_SECONDS 兜底
            if waited_seconds >= MAX_WAIT_SECONDS:
                raise RuntimeError(
                    f"DashScope 查询任务状态持续失败（{MAX_WAIT_SECONDS // 60} 分钟），已超时放弃"
                ) from e
            time.sleep(POLL_INTERVAL_SECONDS)
            waited_seconds += POLL_INTERVAL_SECONDS
            continue

        status = data.get('output', {}).get('task_status', '')

        if status == 'SUCCEEDED':
            return data
        if status in TERMINAL_FAIL_STATUSES:
            msg = data.get('output', {}).get('message', '未知错误')
            raise RuntimeError(f"DashScope 转写任务失败（状态 {status}）: {msg}")

        poll_count += 1
        if progress_callback:
            pct = min(85, 20 + poll_count * 5)
            progress_callback(pct)

        if waited_seconds >= MAX_WAIT_SECONDS:
            raise RuntimeError(
                f"DashScope 转写任务超时（{MAX_WAIT_SECONDS // 60} 分钟内未完成），最后状态: {status or '未知'}"
            )

        time.sleep(POLL_INTERVAL_SECONDS)
        waited_seconds += POLL_INTERVAL_SECONDS


def _parse_result(data):
    """Parse DashScope transcription result into segment list."""
    segments = []

    results_list = data.get('output', {}).get('results', [])
    if not results_list:
        return segments

    first_result = results_list[0]
    if first_result.get('subtask_status') != 'SUCCEEDED':
        return segments

    transcription_url = first_result.get('transcription_url')
    if not transcription_url:
        return segments

    resp = http_requests.get(transcription_url, timeout=30)
    resp.raise_for_status()
    result_data = resp.json()

    transcripts = result_data.get('transcripts', [])
    for transcript in transcripts:
        sentences = transcript.get('sentences', [])
        for sent in sentences:
            begin_ms = sent.get('begin_time', 0)
            text = sent.get('text', '').strip()
            if not text:
                continue

            total_seconds = begin_ms // 1000
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60

            seg = {
                'timestamp': f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                'text': text,
            }
            # 开启说话人分离时会带 speaker_id（通常是 0/1/2…）
            speaker_id = sent.get('speaker_id')
            if speaker_id is not None:
                seg['speaker'] = speaker_id
            segments.append(seg)

    if not segments and transcripts:
        full_text = transcripts[0].get('text', '').strip()
        if full_text:
            segments.append({'timestamp': '00:00:00', 'text': full_text})

    return segments
