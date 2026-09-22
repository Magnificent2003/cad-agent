from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    cad_api_base_url: str = "http://127.0.0.1:8000"
    cad_internal_token: SecretStr = SecretStr("")
    agent_base_url: str = Field(default="http://127.0.0.1:4096", validation_alias=AliasChoices("agent_base_url", "opencode_base_url"))
    agent_server_username: str = Field(default="cad-agent", validation_alias=AliasChoices("agent_server_username", "opencode_server_username"))
    agent_server_password: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("agent_server_password", "opencode_server_password"))
    cad_data_dir: Path = ROOT / "data"
    oda_executable: str = ""
    oda_use_xvfb: bool = False
    cad_font_dirs: str = ""
    cad_fallback_font: str = "NotoSansCJKsc-Regular.otf"
    cad_export_tolerance: float = Field(default=1e-7, gt=0, le=0.001)
    cad_conversion_timeout: int = Field(default=120, ge=10, le=600)
    cad_job_timeout: int = Field(default=300, ge=30, le=1800)
    cad_max_upload_mb: int = Field(default=100, ge=1, le=1024)
    cad_preview_dpi: int = Field(default=120, ge=72, le=300)

    @property
    def data_dir(self) -> Path:
        p = self.cad_data_dir.expanduser()
        return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

    def cad_settings(self) -> dict:
        return {
            "oda_executable": str((ROOT / self.oda_executable).resolve()) if self.oda_executable else "",
            "conversion_timeout": self.cad_conversion_timeout,
            "font_dirs": [str((ROOT / Path(p).expanduser()).resolve()) for p in self.cad_font_dirs.split(":") if p],
            "preview_dpi": self.cad_preview_dpi,
            "oda_use_xvfb": self.oda_use_xvfb,
            "fallback_font": self.cad_fallback_font,
            "export_tolerance": self.cad_export_tolerance,
        }

    @property
    def converter_available(self) -> bool:
        if not self.oda_executable:
            return False
        path = Path(self.cad_settings()["oda_executable"])
        if path == ROOT / "cad_converter/run.sh":
            return path.is_file() and (ROOT / "cad_converter/runtime/squashfs-root/usr/bin/ODAFileConverter").is_file()
        return path.is_file()


@lru_cache
def get_settings() -> Settings:
    return Settings()
