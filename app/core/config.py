from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://localhost/ez_backend"

    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""

    workspace_api_base: str = "https://workspace.invalid"
    workspace_api_key: str = ""
    flip_api_base: str = "https://flip.invalid"
    flip_api_key: str = ""
    translate_api_base: str = "https://translate.invalid"
    translate_api_key: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # ngrok-free.dev entry: Office Web needs the taskpane served over a
    # public origin (both to satisfy Chrome's Private Network Access check
    # and because desktop Office here predates SharedRuntime support), so
    # the dev-server is tunneled through the account's one reserved
    # ngrok-free.dev domain for that path. Kept alongside localhost:3000
    # for direct desktop testing once Office is updated.
    addin_origins: list[str] = [
        "https://localhost:3000",
        "https://gurgling-outdoors-bullpen.ngrok-free.dev",
    ]

    # KD6: poll-timeout ceiling and recurring sweep interval.
    poll_timeout_ceiling_seconds: int = 600
    sweep_interval_seconds: int = 120

    # KD7: grace window past `exp` for GET /tools/jobs/{id} only.
    session_expired_grace_seconds: int = 1800

    # KD1: synchronous Workspace provisioning call timeout.
    workspace_provisioning_timeout_seconds: float = 15.0


settings = Settings()
