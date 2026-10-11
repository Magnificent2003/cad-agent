"""Select one configured model provider; never borrow another provider's key."""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelProvider:
    name: str
    model: str
    base_url: str
    key_name: str
    network_mode: str
    context_limit: int
    output_limit: int

    @property
    def engine_id(self):
        return self.name + "-cad"


def select_provider(env: dict[str, str]) -> ModelProvider:
    name = (env.get("LLM_PROVIDER") or "deepseek").strip().lower()
    if name not in {"deepseek", "aiapi"}:
        raise ValueError("LLM_PROVIDER 只能是 deepseek 或 aiapi；不会自动切换厂商。")
    prefix = name.upper()
    defaults = ("https://api.deepseek.com", "deepseek-flash", 1_000_000) if name == "deepseek" else (
        "https://aiapi.world/v1", "gpt-6.1-sol", 128_000)
    base = (env.get(prefix + "_BASE_URL") or defaults[0]).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(prefix + "_BASE_URL 必须为不含凭据、查询串的 HTTP(S) API 地址。")
    mode = (env.get(prefix + "_NETWORK_MODE") or "auto").strip().lower()
    if mode not in {"auto", "direct", "proxy"}:
        raise ValueError(prefix + "_NETWORK_MODE 只能是 auto、direct 或 proxy。")
    try:
        context = int(env.get(prefix + "_CONTEXT_LIMIT") or defaults[2])
        output = int(env.get(prefix + "_OUTPUT_LIMIT") or 32_000)
    except (TypeError, ValueError):
        raise ValueError(prefix + " 的 CONTEXT_LIMIT / OUTPUT_LIMIT 必须为整数。") from None
    if not 1024 <= output < context:
        raise ValueError(prefix + " 预算要求 1024 <= OUTPUT_LIMIT < CONTEXT_LIMIT。")
    return ModelProvider(name, (env.get(prefix + "_MODEL") or defaults[1]).strip(), base,
                         prefix + "_API_KEY", mode, context, output)


def required_credentials(env: dict[str, str]) -> list[str]:
    provider = select_provider(env)
    return [key for key in (provider.key_name, "AGENT_SERVER_PASSWORD") if not env.get(key, "").strip()]
