"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid


OPENROUTER_MIN_INTERVAL_SECONDS = 3.2
MAX_HTTP_ATTEMPTS = 4
MAX_EMPTY_RESPONSE_RETRIES = 3

_request_pacing_lock = threading.Lock()
_last_request_started_at = 0.0

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


def _pace_request(min_interval):
    """Serialize request starts so concurrent callers also respect the limit."""
    global _last_request_started_at

    if min_interval <= 0:
        return

    with _request_pacing_lock:
        elapsed = time.monotonic() - _last_request_started_at
        remaining = min_interval - elapsed

        if remaining > 0:
            time.sleep(remaining)

        _last_request_started_at = time.monotonic()


def _retry_after_seconds(exc, attempt):
    """Return a bounded delay without exposing the provider response body."""
    retry_after = exc.headers.get('Retry-After') if exc.headers else None

    try:
        requested_delay = float(retry_after)
    except (TypeError, ValueError):
        requested_delay = 0.0

    exponential_delay = 5.0 * (2 ** attempt)
    return min(60.0, max(exponential_delay, requested_delay))


def post_json(url, headers, payload, min_interval=0.0):
    for attempt in range(MAX_HTTP_ATTEMPTS):
        _pace_request(min_interval)

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                **headers,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)

        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_HTTP_ATTEMPTS - 1:
                wait_seconds = _retry_after_seconds(exc, attempt)
                print(
                    f'Rate limit 429; retrying in {wait_seconds:g}s...'
                )
                time.sleep(wait_seconds)
                continue

            raise RuntimeError(
                f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.'
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError('Không kết nối được provider.') from exc


def _assistant_message_is_empty(data):
    """A refusal or tool call is meaningful even when content is empty."""
    try:
        message = data['choices'][0]['message']
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError('Provider trả response không hợp lệ.') from exc

    if not isinstance(message, dict):
        raise RuntimeError('Provider trả assistant message không hợp lệ.')

    content = message.get('content')
    has_content = bool(content.strip()) if isinstance(content, str) else bool(content)
    return not (
        has_content
        or message.get('tool_calls')
        or message.get('refusal')
    )


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        self.session_id = str(uuid.uuid4())

        key_name, default_url = CONFIG[name]
        custom_endpoint = os.getenv('API_ENDPOINT')

        self.url = custom_endpoint or default_url

        if self.url.rstrip('/').endswith('/v1'):
            self.url = self.url.rstrip('/') + '/chat/completions'

        if 'opencode.ai' in self.url:
            key_name = 'OPENCODE_GO_API_KEY'

        self.key = os.getenv(key_name)

        if self.key:
            self.key = self.key.strip()

        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)

        payload = {
            'model': self.model,
            'messages': messages,
        }

        if self.model in ('gpt-6-luna', 'openai/gpt-6-luna'):
            payload['reasoning_effort'] = 'none'

        if schemas:
            payload['tools'] = schemas

        headers = {
            'Authorization': f'Bearer {self.key}',
        }

        if 'opencode.ai' in self.url:
            headers['User-Agent'] = 'StatusAgentLab/1.0'
            headers['x-opencode-session'] = self.session_id

        min_interval = (
            OPENROUTER_MIN_INTERVAL_SECONDS
            if self.name == 'openrouter'
            else 0.0
        )

        for empty_retry in range(MAX_EMPTY_RESPONSE_RETRIES + 1):
            data = post_json(
                self.url,
                headers,
                payload,
                min_interval,
            )

            if not _assistant_message_is_empty(data):
                break

            if empty_retry == MAX_EMPTY_RESPONSE_RETRIES:
                raise RuntimeError(
                    'Provider trả assistant message rỗng sau nhiều lần thử.'
                )

            print('Empty assistant message; retrying...')

        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
