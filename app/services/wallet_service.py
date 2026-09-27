from app.core.errors import WorkspaceUnprovisionedError
from app.models.user import User
from app.services.workspace_client import WorkspaceClient


async def get_balance(user: User, workspace_client: WorkspaceClient) -> int:
    """R4/R5: thin passthrough to workspace_client -- no local balance math,
    no local ledger row. Short-circuits before any Workspace call when the
    wallet isn't provisioned yet (KD4).
    """
    if user.workspace_account_status != "active" or not user.ez_wallet_id:
        raise WorkspaceUnprovisionedError()
    return await workspace_client.get_balance(user.ez_wallet_id)
