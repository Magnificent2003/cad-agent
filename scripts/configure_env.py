"""Fill local service credentials without printing or replacing existing secrets."""
from pathlib import Path
import secrets

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    template = ROOT / ".env.example"
    if not target.exists():
        target.write_text(template.read_text(), encoding="utf-8")
    current = dotenv_values(target)
    # Preserve credentials when upgrading a checkout with the old variable names.
    for suffix in ("BASE_URL", "SERVER_USERNAME", "SERVER_PASSWORD"):
        old, new = "OPENCODE_" + suffix, "AGENT_" + suffix
        if not current.get(new) and current.get(old):
            set_key(target, new, current[old])
            current[new] = current[old]
    for key, value in dotenv_values(template).items():
        if key not in current:
            set_key(target, key, value or "")
            current[key] = value or ""
    for key in ("AGENT_SERVER_PASSWORD",):
        if not current.get(key):
            set_key(target, key, secrets.token_urlsafe(32))
    target.chmod(0o600)
    print("环境字段已同步，本地服务凭据已配置。")
    selected = (current.get("LLM_PROVIDER") or "deepseek").strip().upper()
    if not current.get(selected + "_API_KEY"):
        print(f"请在 .env 中配置 {selected}_API_KEY 以启用所选模型。")


if __name__ == "__main__":
    main()
