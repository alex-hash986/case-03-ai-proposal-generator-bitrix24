import asyncio
from base64 import b64encode
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from src.core.config import Settings
from src.core.database import Database
from src.domain.ports import UncertainWrite


class RateLimiter:
    def __init__(self, interval: float = 0.5, *, cold_start: bool = False) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next_request = monotonic() + interval if cold_start else 0.0

    def cooldown(self) -> None:
        self._next_request = monotonic() + self.interval

    async def wait(self) -> None:
        async with self._lock:
            while (remaining := self._next_request - monotonic()) > 0:
                await asyncio.sleep(remaining)
            self.cooldown()


class MockBitrixConnector:
    def __init__(self, database: Database, *, interval: float = 0.5) -> None:
        self.database = database
        self.limiter = RateLimiter(interval)

    async def _pace(self) -> None:
        await self.limiter.wait()

    async def read_deal(self, deal_id: int, *, key: str = "") -> dict[str, Any]:
        await self._pace()
        return {"deal_id": deal_id, "stage": "PREPARATION", "source": "mock"}

    async def apply(
        self, key: str, operation: str, payload: dict[str, Any], *, simulate_failure: bool,
    ) -> None:
        await self._pace()
        inserted = self.database.mock_operation(key, operation, payload)
        if inserted and simulate_failure:
            raise UncertainWrite("Ответ потерян после фиксации операции в CRM")


class ConfirmedRejection(UncertainWrite):
    """The request could not have produced a remote mutation."""


class LiveBitrixConnector:
    def __init__(self, settings: Settings, database: Database, client: httpx.AsyncClient) -> None:
        secret = settings.bitrix_webhook_url
        if secret is None or settings.bitrix_director_id is None:
            raise ValueError("Live Bitrix24 requires BITRIX_WEBHOOK_URL and BITRIX_DIRECTOR_ID")
        url = httpx.URL(secret.get_secret_value())
        parts = url.path.strip("/").split("/")
        if (
            url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or len(parts) != 3 or parts[0] != "rest" or not parts[1].isdigit() or not parts[2]
        ):
            raise ValueError("Invalid Bitrix24 HTTPS webhook URL")
        self.url = str(url).rstrip("/") + "/"
        self.scope = sha256(f"{url.host}:{url.port}".encode()).hexdigest()
        self.director_id = settings.bitrix_director_id
        self.stage = settings.bitrix_proposal_stage
        self.database = database
        self.client = client
        self.limiter = RateLimiter(cold_start=True)
        self._request_lock = asyncio.Lock()

    async def _request(
        self, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._request_lock:
            await self.limiter.wait()
            try:
                response = await self.client.post(self.url + method, json=params, follow_redirects=False)
                data = response.json()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                raise ConfirmedRejection(f"Bitrix24 {method}: connection not established") from None
            except (httpx.HTTPError, ValueError):
                raise UncertainWrite(f"Bitrix24 {method}: no verifiable response") from None
            finally:
                self.limiter.cooldown()
        if not isinstance(data, dict):
            raise UncertainWrite(f"Bitrix24 {method}: invalid response")
        rejected = {
            "ACCESS_DENIED", "ERROR_ACCESS_DENIED", "NO_AUTH_FOUND", "INVALID_CREDENTIALS",
            "QUERY_LIMIT_EXCEEDED", "INSUFFICIENT_SCOPE",
        }
        if str(data.get("error")).upper() in rejected and response.status_code < 500:
            raise ConfirmedRejection(f"Bitrix24 {method}: request rejected")
        if not response.is_success or "result" not in data or data.get("error"):
            raise UncertainWrite(f"Bitrix24 {method}: outcome inconclusive")
        return data

    async def _read(
        self, key: str, step: str, method: str, params: dict[str, Any], *, cache: bool = False,
    ) -> dict[str, Any]:
        state, stored = self.database.bitrix_state(key, self.scope, step)
        if cache and state == "done":
            assert isinstance(stored, dict)
            return stored
        self.database.bitrix_save(key, self.scope, step, "started")
        response = await self._request(method, params)
        self.database.bitrix_save(key, self.scope, step, "done", response)
        return response

    async def read_deal(self, deal_id: int, *, key: str) -> dict[str, Any]:
        deal = (await self._read(key, "deal", "crm.deal.get", {"id": deal_id}, cache=True))["result"]
        if not isinstance(deal, dict) or str(deal.get("ID")) != str(deal_id):
            raise UncertainWrite("Bitrix24: no matching deal")
        if deal.get("CURRENCY_ID") != "RUB" or not str(deal.get("ASSIGNED_BY_ID")).isdigit():
            raise UncertainWrite("Bitrix24: expected RUB and an assigned manager")
        if int(deal["ASSIGNED_BY_ID"]) <= 0:
            raise UncertainWrite("Bitrix24: manager is missing")
        rows = (await self._read(
            key, "products", "crm.deal.productrows.get", {"id": deal_id}, cache=True,
        ))["result"]
        company_id = int(deal.get("COMPANY_ID") or 0)
        contact_id = int(deal.get("CONTACT_ID") or 0)
        requisites = []
        if company_id or contact_id:
            requisites = (await self._read(
                key, "requisites", "crm.requisite.list",
                {"filter": {"ENTITY_TYPE_ID": 4 if company_id else 3,
                            "ENTITY_ID": company_id or contact_id}},
                cache=True,
            ))["result"]
        return {"deal": deal, "product_rows": rows, "requisites": requisites}

    async def _find(
        self, key: str, operation: str, deal_id: int, marker: str, title: str,
    ) -> str | None:
        is_comment = operation == "timeline"
        method = "crm.timeline.comment.list" if is_comment else "tasks.task.list"
        start = 0
        while True:
            params: dict[str, Any] = (
                {"filter": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"},
                 "select": ["ID", "COMMENT", "FILES"], "order": {"ID": "DESC"}}
                if is_comment else {"filter": {"TITLE": title}, "select": ["ID", "TITLE"]}
            )
            response = await self._read(
                key, f"{operation}.lookup.{start}", method, params | {"start": start},
            )
            rows = response["result"] if is_comment else response["result"]["tasks"]
            if not isinstance(rows, list):
                raise UncertainWrite(f"Bitrix24 {method}: invalid list")
            for item in rows:
                if is_comment and str(item.get("COMMENT", "")).splitlines()[-1:] == [marker]:
                    if not item.get("FILES"):
                        raise UncertainWrite("Bitrix24: found comment without PDF attachment")
                    return str(item["ID"])
                if not is_comment and item.get("title") == title:
                    return str(item["id"])
            next_page = response.get("next")
            if next_page is None:
                return None
            if type(next_page) is not int or next_page <= start:
                raise UncertainWrite(f"Bitrix24 {method}: invalid pagination")
            start = next_page

    async def apply(
        self, key: str, operation: str, payload: dict[str, Any], *, simulate_failure: bool,
    ) -> None:
        if operation == "upload_pdf":
            # Timeline FILES uploads and attaches the PDF in a single REST operation.
            return
        state, _ = self.database.bitrix_state(key, self.scope, operation)
        if state == "done":
            return
        deal_id = int(payload["deal_id"])
        marker = f"[КП {sha256(key.encode()).hexdigest()[:24]}]"
        title = f"{'Проверить КП' if payload.get('action') == 'review' else 'Связаться по КП'} {marker}"
        if operation == "update_deal":
            method = "crm.deal.update"
            fields = {"OPPORTUNITY": payload["amount"], "IS_MANUAL_OPPORTUNITY": "Y"}
            if self.stage:
                fields["STAGE_ID"] = self.stage
            if state == "started":
                remote = (await self._read(
                    key, "update_deal.lookup", "crm.deal.get", {"id": deal_id},
                ))["result"]
                try:
                    matches = Decimal(str(remote["OPPORTUNITY"])) == Decimal(fields["OPPORTUNITY"])
                except (KeyError, InvalidOperation):
                    matches = False
                if (
                    matches and remote.get("IS_MANUAL_OPPORTUNITY") == "Y"
                    and (not self.stage or remote.get("STAGE_ID") == self.stage)
                ):
                    self.database.bitrix_save(key, self.scope, operation, "done", True)
                    return
                raise UncertainWrite("Bitrix24: ambiguous deal update requires portal verification")
            params = {"id": deal_id, "fields": fields}
        else:
            if state == "started":
                existing = await self._find(key, operation, deal_id, marker, title)
                if existing is None:
                    raise UncertainWrite(f"Bitrix24 {operation}: pending remote confirmation")
                self.database.bitrix_save(key, self.scope, operation, "done", existing)
                return
            if operation == "timeline":
                method = "crm.timeline.comment.add"
                try:
                    encoded = b64encode(Path(payload["pdf_path"]).read_bytes()).decode("ascii")
                except OSError:
                    raise ConfirmedRejection("PDF could not be read before upload") from None
                note = "Внутренний черновик КП" if payload["internal_draft"] else "Коммерческое предложение"
                reasons = "\n".join(payload["reasons"])
                params = {"fields": {
                    "ENTITY_ID": deal_id, "ENTITY_TYPE": "deal",
                    "COMMENT": f"{note}\n{payload['summary']}\nИтого {payload['total_rub']} ₽\n"
                    f"{reasons}\n{marker}",
                    "FILES": [[f"proposal-{deal_id}-{marker[4:-1]}.pdf", encoded]],
                }}
            else:
                method = "tasks.task.add"
                params = {"fields": {
                    "TITLE": title,
                    "RESPONSIBLE_ID": self.director_id if payload["assignee"] == "director"
                    else int(payload["assigned_by"]),
                    "UF_CRM_TASK": [f"D_{deal_id}"],
                }}
        self.database.bitrix_save(key, self.scope, operation, "started")
        try:
            result = (await self._request(method, params))["result"]
        except ConfirmedRejection:
            self.database.bitrix_save(key, self.scope, operation, "retry")
            raise
        if operation == "task":
            task = result.get("task") if isinstance(result, dict) else None
            remote_id = task.get("id") if isinstance(task, dict) else None
        else:
            remote_id = result
        if operation == "update_deal":
            confirmed = remote_id is True
        else:
            confirmed = str(remote_id).isascii() and str(remote_id).isdigit() and int(str(remote_id)) > 0
        if not confirmed:
            raise UncertainWrite(f"Bitrix24 {method}: no confirmation")
        self.database.bitrix_save(key, self.scope, operation, "done", remote_id)
