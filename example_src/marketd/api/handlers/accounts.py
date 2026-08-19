"""``/v1/accounts`` - opening accounts, funding them, reading balances."""

from __future__ import annotations

from ...http.message import Request, Response
from ..schemas import CREATE_ACCOUNT, CREATE_DEPOSIT, present_account, present_balance


def register(router, container) -> None:
    accounts = container.account_service

    async def create_account(request: Request) -> Response:
        payload = CREATE_ACCOUNT(request.json(), "$")
        account, api_key = accounts.create(payload["name"], payload["tier"])
        body = present_account(account)
        # Shown once and never again - the server keeps only a digest.
        body["api_key"] = api_key
        return Response.json(body, status=201, headers={"location": f"/v1/accounts/{account.id}"})

    async def get_account(request: Request) -> Response:
        account = accounts.authorise(
            request.ctx["principal"], request.path_params["account_id"]
        )
        return Response.json(present_account(account))

    async def get_balances(request: Request) -> Response:
        account_id = request.path_params["account_id"]
        accounts.authorise(request.ctx["principal"], account_id)
        return Response.json(
            {"data": [present_balance(balance) for balance in accounts.balances(account_id)]}
        )

    async def create_deposit(request: Request) -> Response:
        account_id = request.path_params["account_id"]
        principal = request.ctx["principal"]
        accounts.authorise(principal, account_id)
        principal.require("trade")
        payload = CREATE_DEPOSIT(request.json(), "$")
        receipt = accounts.deposit(account_id, payload["asset"], payload["amount"])
        return Response.json(receipt, status=201)

    # Account creation is public: it is how a caller gets its first key.
    router.post("/v1/accounts", create_account, name="create_account")
    router.get("/v1/accounts/{account_id}", get_account, name="get_account")
    router.get("/v1/accounts/{account_id}/balances", get_balances, name="get_balances")
    router.post("/v1/accounts/{account_id}/deposits", create_deposit, name="create_deposit")
