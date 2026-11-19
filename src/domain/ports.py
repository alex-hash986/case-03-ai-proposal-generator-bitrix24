from typing import Any, Protocol


class UncertainWrite(ConnectionError):
    pass


class BitrixPort(Protocol):
    async def read_deal(self, deal_id: int, *, key: str) -> dict[str, Any]: ...

    async def apply(
        self, key: str, operation: str, payload: dict[str, Any], *, simulate_failure: bool,
    ) -> None: ...
