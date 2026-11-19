from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ROOT = Path(__file__).resolve().parents[2]
Percent = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]
Rate = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class DiscountPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_discount_pct: Percent
    min_margin_pct: Percent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", yaml_file=ROOT / "config/settings.yaml",
    )
    api_port: int = 8013
    ui_port: int = 8503
    database: Path
    artifacts: Path
    template: Path
    brand: str
    pdf_font: Path | None = None
    vat_pct: Percent
    discount_matrix: dict[str, DiscountPolicy]
    max_cycle_cost_rub: Rate
    llm_mode: Literal["mock", "live"] = "mock"
    bitrix_mode: Literal["mock", "live"] = "mock"
    bitrix_webhook_url: SecretStr | None = None
    bitrix_director_id: Annotated[int, Field(gt=0)] | None = None
    bitrix_proposal_stage: str | None = None
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: SecretStr = SecretStr("ollama")
    fast_llm_model: str = "qwen2.5:7b"
    smart_llm_model: str = "qwen2.5:14b"
    embedding_model: str = "bge-m3"
    llm_rub_per_million_input_tokens: Rate | None = None
    llm_rub_per_million_output_tokens: Rate | None = None
    embedding_rub_per_million_input_tokens: Rate | None = None
    ab_variant: Literal["variant_a", "variant_b"] = "variant_a"
    manual_baseline_minutes: Rate | None = None

    @classmethod
    def settings_customise_sources(
        cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource, dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, dotenv_settings, YamlConfigSettingsSource(settings_cls)


def load_settings(**overrides: object) -> Settings:
    settings = Settings(**overrides)  # type: ignore[arg-type]
    return settings.model_copy(update={
        "database": ROOT / settings.database,
        "artifacts": ROOT / settings.artifacts,
        "template": ROOT / settings.template,
    })
