"""Select the model network route explicitly instead of inheriting a fragile proxy.

The API address/key are unchanged. Local agent/MCP traffic always bypasses proxies.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx


class ModelNetworkError(RuntimeError):
    pass


def _bypass(env: dict[str, str], *hosts: str) -> dict[str, str]:
    result = dict(env)
    entries = []
    for value in (env.get('NO_PROXY', ''), env.get('no_proxy', ''), *hosts):
        for host in value.split(','):
            if host.strip() and host.strip() not in entries:
                entries.append(host.strip())
    result['NO_PROXY'] = result['no_proxy'] = ','.join(entries)
    return result


def prepare_model_network(env: dict[str, str]) -> tuple[dict[str, str], str]:
    """One authenticated, non-generating API request per candidate startup route."""
    result = _bypass(env, 'localhost', '127.0.0.1', '::1')
    base = result.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
    host = urlsplit(base).hostname
    if not host:
        raise ModelNetworkError('模型 API 地址无效。')
    mode = result.get('DEEPSEEK_NETWORK_MODE', 'auto').lower().strip()
    if mode not in {'auto', 'direct', 'proxy'}:
        raise ModelNetworkError('DEEPSEEK_NETWORK_MODE 只能是 auto、direct 或 proxy。')
    proxy = (result.get('https_proxy') or result.get('HTTPS_PROXY') or
             result.get('all_proxy') or result.get('ALL_PROXY'))
    routes = [('direct', None)] if mode == 'direct' else [('proxy', proxy)] if mode == 'proxy' else [('direct', None), ('proxy', proxy)]
    errors = []
    for route, address in routes:
        if route == 'proxy' and not address:
            errors.append('代理：未配置 HTTPS_PROXY / ALL_PROXY')
            continue
        try:
            with httpx.Client(proxy=address, trust_env=False, timeout=httpx.Timeout(8, connect=4)) as client:
                response = client.get(base + '/models', headers={'Authorization': 'Bearer ' + result['DEEPSEEK_API_KEY']})
        except ImportError:
            errors.append('代理：当前环境不支持 SOCKS，请使用 HTTP 代理地址')
            continue
        except (httpx.HTTPError, ValueError):
            # Exception details can contain proxy credentials. Keep diagnostics bounded and safe.
            errors.append(('直连' if route == 'direct' else '代理') + '：无法建立有效连接')
            continue
        if response.status_code == 401:
            raise ModelNetworkError(f'模型 API 已连接，但认证被拒绝（HTTP {response.status_code}）；不是端口占用或 CAD 工具错误。')
        if response.status_code != 200:
            errors.append(('直连' if route == 'direct' else '代理') + f'：HTTP {response.status_code}')
            continue
        try:
            models = [item['id'] for item in response.json()['data']]
        except (ValueError, KeyError, TypeError):
            errors.append(('直连' if route == 'direct' else '代理') + '：API 返回格式不正确')
            continue
        model = result.get('DEEPSEEK_MODEL', 'deepseek-flash').strip() or 'deepseek-flash'
        if model not in models:
            raise ModelNetworkError(f'模型 API 已连接，但没有配置的模型 {model}。')
        if route == 'direct':
            result = _bypass(result, host)
            return result, f'模型 API 已连接：{host}（直连，不依赖本地代理）'
        # Make all proxy spellings agree, and ensure the tested endpoint does not
        # accidentally bypass the selected proxy because of inherited NO_PROXY.
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            result[name] = address
        result['NO_PROXY'] = result['no_proxy'] = 'localhost,127.0.0.1,::1'
        return result, f'模型 API 已连接：{host}（配置的代理）'
    raise ModelNetworkError('模型 API 无法连接；' + '；'.join(errors) + '。API 地址与密钥未改动，请检查网络或代理。')
