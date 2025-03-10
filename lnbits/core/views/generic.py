import asyncio
from http import HTTPStatus
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import Depends, Query, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRouter
from loguru import logger
from pydantic.types import UUID4

from lnbits.core.db import db
from lnbits.core.helpers import to_valid_user_id
from lnbits.core.models import User
from lnbits.decorators import check_admin, check_user_exists
from lnbits.helpers import template_renderer, url_for
from lnbits.settings import settings
from lnbits.wallets import get_wallet_class

from ...extension_manager import InstallableExtension, get_valid_extensions
from ...utils.exchange_rates import currencies
from ..crud import (
    create_account,
    create_wallet,
    get_balance_check,
    get_dbversions,
    get_inactive_extensions,
    get_installed_extensions,
    get_user,
    save_balance_notify,
    update_installed_extension_state,
    update_user_extension,
)
from ..services import pay_invoice, redeem_lnurl_withdraw

generic_router = APIRouter(
    tags=["Core NON-API Website Routes"], include_in_schema=False
)


@generic_router.get("/favicon.ico", response_class=FileResponse)
async def favicon():
    return FileResponse("lnbits/core/static/favicon.ico")


@generic_router.get("/", response_class=HTMLResponse)
async def home(request: Request, lightning: str = ""):
    return template_renderer().TemplateResponse(
        "core/index.html", {"request": request, "lnurl": lightning}
    )


@generic_router.get("/robots.txt", response_class=HTMLResponse)
async def robots():
    data = """
    User-agent: *
    Disallow: /
    """
    return HTMLResponse(content=data, media_type="text/plain")


@generic_router.get(
    "/extensions", name="install.extensions", response_class=HTMLResponse
)
async def extensions_install(
    request: Request,
    user: User = Depends(check_user_exists),
    activate: str = Query(None),
    deactivate: str = Query(None),
    enable: str = Query(None),
    disable: str = Query(None),
):
    await toggle_extension(enable, disable, user.id)

    # Update user as his extensions have been updated
    if enable or disable:
        user = await get_user(user.id)  # type: ignore
    try:
        installed_exts: List["InstallableExtension"] = await get_installed_extensions()
        installed_exts_ids = [e.id for e in installed_exts]

        installable_exts: List[
            InstallableExtension
        ] = await InstallableExtension.get_installable_extensions()
        installable_exts += [
            e for e in installed_exts if e.id not in installed_exts_ids
        ]

        for e in installable_exts:
            installed_ext = next((ie for ie in installed_exts if e.id == ie.id), None)
            if installed_ext:
                e.installed_release = installed_ext.installed_release
                # use the installed extension values
                e.name = installed_ext.name
                e.short_description = installed_ext.short_description
                e.icon = installed_ext.icon

    except Exception as ex:
        logger.warning(ex)
        installable_exts = []

    try:
        ext_id = activate or deactivate
        if ext_id and user.admin:
            if deactivate and deactivate not in settings.lnbits_deactivated_extensions:
                settings.lnbits_deactivated_extensions += [deactivate]
            elif activate:
                settings.lnbits_deactivated_extensions = list(
                    filter(
                        lambda e: e != activate, settings.lnbits_deactivated_extensions
                    )
                )
            await update_installed_extension_state(
                ext_id=ext_id, active=activate is not None
            )

        all_extensions = list(map(lambda e: e.code, get_valid_extensions()))
        inactive_extensions = await get_inactive_extensions()
        db_version = await get_dbversions()
        extensions = list(
            map(
                lambda ext: {
                    "id": ext.id,
                    "name": ext.name,
                    "icon": ext.icon,
                    "shortDescription": ext.short_description,
                    "stars": ext.stars,
                    "isFeatured": ext.featured,
                    "dependencies": ext.dependencies,
                    "isInstalled": ext.id in installed_exts_ids,
                    "hasDatabaseTables": ext.id in db_version,
                    "isAvailable": ext.id in all_extensions,
                    "isAdminOnly": ext.id in settings.lnbits_admin_extensions,
                    "isActive": ext.id not in inactive_extensions,
                    "latestRelease": (
                        dict(ext.latest_release) if ext.latest_release else None
                    ),
                    "installedRelease": (
                        dict(ext.installed_release) if ext.installed_release else None
                    ),
                },
                installable_exts,
            )
        )

        return template_renderer().TemplateResponse(
            "core/extensions.html",
            {
                "request": request,
                "user": user.dict(),
                "extensions": extensions,
            },
        )
    except Exception as e:
        logger.warning(e)
        raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR, detail=str(e))


@generic_router.get(
    "/wallet",
    response_class=HTMLResponse,
    description="show wallet page",
)
async def wallet(
    request: Request,
    usr: UUID4 = Query(...),
    wal: Optional[UUID4] = Query(None),
):
    user_id = usr.hex
    user = await get_user(user_id)

    if not user:
        return template_renderer().TemplateResponse(
            "error.html", {"request": request, "err": "User does not exist."}
        )

    if not wal:
        if len(user.wallets) == 0:
            wallet = await create_wallet(user_id=user.id)
            return RedirectResponse(url=f"/wallet?usr={user_id}&wal={wallet.id}")
        return RedirectResponse(url=f"/wallet?usr={user_id}&wal={user.wallets[0].id}")
    else:
        wallet_id = wal.hex

    userwallet = user.get_wallet(wallet_id)
    if not userwallet or userwallet.deleted:
        return template_renderer().TemplateResponse(
            "error.html", {"request": request, "err": "Wallet not found"}
        )

    if (
        len(settings.lnbits_allowed_users) > 0
        and user_id not in settings.lnbits_allowed_users
        and user_id not in settings.lnbits_admin_users
        and user_id != settings.super_user
    ):
        return template_renderer().TemplateResponse(
            "error.html", {"request": request, "err": "User not authorized."}
        )

    if user_id == settings.super_user or user_id in settings.lnbits_admin_users:
        user.admin = True
    if user_id == settings.super_user:
        user.super_user = True

    logger.debug(f"Access user {user.id} wallet {userwallet.name}")

    return template_renderer().TemplateResponse(
        "core/wallet.html",
        {
            "request": request,
            "user": user.dict(),
            "wallet": userwallet.dict(),
            "service_fee": settings.lnbits_service_fee,
            "web_manifest": f"/manifest/{user.id}.webmanifest",
        },
    )


@generic_router.get("/withdraw", response_class=JSONResponse)
async def lnurl_full_withdraw(request: Request):
    usr_param = request.query_params.get("usr")
    if not usr_param:
        return {"status": "ERROR", "reason": "usr parameter not provided."}

    user = await get_user(usr_param)
    if not user:
        return {"status": "ERROR", "reason": "User does not exist."}

    wal_param = request.query_params.get("wal")
    if not wal_param:
        return {"status": "ERROR", "reason": "wal parameter not provided."}

    wallet = user.get_wallet(wal_param)
    if not wallet:
        return {"status": "ERROR", "reason": "Wallet does not exist."}

    return {
        "tag": "withdrawRequest",
        "callback": url_for("/withdraw/cb", external=True, usr=user.id, wal=wallet.id),
        "k1": "0",
        "minWithdrawable": 1000 if wallet.withdrawable_balance else 0,
        "maxWithdrawable": wallet.withdrawable_balance,
        "defaultDescription": (
            f"{settings.lnbits_site_title} balance withdraw from {wallet.id[0:5]}"
        ),
        "balanceCheck": url_for("/withdraw", external=True, usr=user.id, wal=wallet.id),
    }


@generic_router.get("/withdraw/cb", response_class=JSONResponse)
async def lnurl_full_withdraw_callback(request: Request):
    usr_param = request.query_params.get("usr")
    if not usr_param:
        return {"status": "ERROR", "reason": "usr parameter not provided."}

    user = await get_user(usr_param)
    if not user:
        return {"status": "ERROR", "reason": "User does not exist."}

    wal_param = request.query_params.get("wal")
    if not wal_param:
        return {"status": "ERROR", "reason": "wal parameter not provided."}

    wallet = user.get_wallet(wal_param)
    if not wallet:
        return {"status": "ERROR", "reason": "Wallet does not exist."}

    pr = request.query_params.get("pr")
    if not pr:
        return {"status": "ERROR", "reason": "payment_request not provided."}

    async def pay():
        try:
            await pay_invoice(wallet_id=wallet.id, payment_request=pr)
        except Exception:
            pass

    asyncio.create_task(pay())

    balance_notify = request.query_params.get("balanceNotify")
    if balance_notify:
        await save_balance_notify(wallet.id, balance_notify)

    return {"status": "OK"}


@generic_router.get("/withdraw/notify/{service}")
async def lnurl_balance_notify(request: Request, service: str):
    wal_param = request.query_params.get("wal")
    if not wal_param:
        return {"status": "ERROR", "reason": "wal parameter not provided."}

    bc = await get_balance_check(wal_param, service)
    if bc:
        await redeem_lnurl_withdraw(bc.wallet, bc.url)


@generic_router.get(
    "/lnurlwallet", response_class=RedirectResponse, name="core.lnurlwallet"
)
async def lnurlwallet(request: Request):
    async with db.connect() as conn:
        account = await create_account(conn=conn)
        user = await get_user(account.id, conn=conn)
        assert user, "Newly created user not found."
        wallet = await create_wallet(user_id=user.id, conn=conn)

    lightning_param = request.query_params.get("lightning")
    if not lightning_param:
        return {"status": "ERROR", "reason": "lightning parameter not provided."}

    asyncio.create_task(
        redeem_lnurl_withdraw(
            wallet.id,
            lightning_param,
            "LNbits initial funding: voucher redeem.",
            {"tag": "lnurlwallet"},
            5,  # wait 5 seconds before sending the invoice to the service
        )
    )

    return RedirectResponse(
        f"/wallet?usr={user.id}&wal={wallet.id}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@generic_router.get("/service-worker.js", response_class=FileResponse)
async def service_worker():
    return FileResponse("lnbits/core/static/js/service-worker.js")


@generic_router.get("/manifest/{usr}.webmanifest")
async def manifest(request: Request, usr: str):
    host = urlparse(str(request.url)).netloc

    user = await get_user(usr)
    if not user:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND)

    return {
        "short_name": settings.lnbits_site_title,
        "name": settings.lnbits_site_title + " Wallet",
        "icons": [
            {
                "src": (
                    settings.lnbits_custom_logo
                    if settings.lnbits_custom_logo
                    else "https://cdn.jsdelivr.net/gh/lnbits/lnbits@main/docs/logos/lnbits.png"
                ),
                "type": "image/png",
                "sizes": "900x900",
            }
        ],
        "start_url": f"/wallet?usr={usr}&wal={user.wallets[0].id}",
        "background_color": "#1F2234",
        "description": "Bitcoin Lightning Wallet",
        "display": "standalone",
        "scope": "/",
        "theme_color": "#1F2234",
        "shortcuts": [
            {
                "name": wallet.name,
                "short_name": wallet.name,
                "description": wallet.name,
                "url": f"/wallet?usr={usr}&wal={wallet.id}",
            }
            for wallet in user.wallets
        ],
        "url_handlers": [{"origin": f"https://{host}"}],
    }


@generic_router.get("/node", response_class=HTMLResponse)
async def node(request: Request, user: User = Depends(check_admin)):
    if not settings.lnbits_node_ui:
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE)

    WALLET = get_wallet_class()
    _, balance = await WALLET.status()

    return template_renderer().TemplateResponse(
        "node/index.html",
        {
            "request": request,
            "user": user.dict(),
            "settings": settings.dict(),
            "balance": balance,
            "wallets": user.wallets[0].dict(),
        },
    )


@generic_router.get("/node/public", response_class=HTMLResponse)
async def node_public(request: Request):
    if not settings.lnbits_public_node_ui:
        raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE)

    WALLET = get_wallet_class()
    _, balance = await WALLET.status()

    return template_renderer().TemplateResponse(
        "node/public.html",
        {
            "request": request,
            "settings": settings.dict(),
            "balance": balance,
        },
    )


@generic_router.get("/admin", response_class=HTMLResponse)
async def index(request: Request, user: User = Depends(check_admin)):
    if not settings.lnbits_admin_ui:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND)

    WALLET = get_wallet_class()
    _, balance = await WALLET.status()

    return template_renderer().TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "user": user.dict(),
            "settings": settings.dict(),
            "balance": balance,
            "currencies": list(currencies.keys()),
        },
    )


@generic_router.get("/uuidv4/{hex_value}")
async def hex_to_uuid4(hex_value: str):
    try:
        user_id = to_valid_user_id(hex_value).hex
        return RedirectResponse(url=f"/wallet?usr={user_id}")
    except Exception as e:
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))


async def toggle_extension(extension_to_enable, extension_to_disable, user_id):
    if extension_to_enable and extension_to_disable:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "You can either `enable` or `disable` an extension."
        )

    # check if extension exists
    if extension_to_enable or extension_to_disable:
        ext = extension_to_enable or extension_to_disable
        if ext not in [e.code for e in get_valid_extensions()]:
            raise HTTPException(
                HTTPStatus.BAD_REQUEST, f"Extension '{ext}' doesn't exist."
            )

    if extension_to_enable:
        logger.info(f"Enabling extension: {extension_to_enable} for user {user_id}")
        await update_user_extension(
            user_id=user_id, extension=extension_to_enable, active=True
        )
    elif extension_to_disable:
        logger.info(f"Disabling extension: {extension_to_disable} for user {user_id}")
        await update_user_extension(
            user_id=user_id, extension=extension_to_disable, active=False
        )
