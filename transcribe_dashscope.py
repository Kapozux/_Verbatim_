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


def transcribe_audio(filepath, progress_callback=None):
    """
    Transcribe audio using DashScope Paraformer via REST API.

    Returns:
        List of segment dicts with keys: timestamp (str), text (str).
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

    task_id = _submit_task(file_url, api_key)

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


def _submit_task(file_url, api_key):
    """Submit a transcription task via REST API and return task_id."""
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
            'parameters': {
                'language_hints': ['zh', 'en'],
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    task_id = data.get('output', {}).get('task_id')
    if not task_id:
        msg = data.get('message', json.dumps(data, ensure_ascii=False))
        raise RuntimeError(f"DashScope 转写任务提交失败: {msg}")

    return task_id


def _poll_task(task_id, api_key, progress_callback=None):
    """Poll transcription task via REST API until completion."""
    poll_count = 0
    while True:
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

        status = data.get('output', {}).get('task_status', '')

        if status == 'SUCCEEDED':
            return data
        if status == 'FAILED':
            msg = data.get('output', {}).get('message', '未知错误')
            raise RuntimeError(f"DashScope 转写任务失败: {msg}")

        poll_count += 1
        if progress_callback:
            pct = min(85, 20 + poll_count * 5)
            progress_callback(pct)

        time.sleep(2)


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

            segments.append({
                'timestamp': f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                'text': text,
            })

    if not segments and transcripts:
        full_text = transcripts[0].get('text', '').strip()
        if full_text:
            segments.append({'timestamp': '00:00:00', 'text': full_text})

    return segments
