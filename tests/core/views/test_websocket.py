import asyncio
import hashlib

import pytest

from lnbits import bolt11
from lnbits.core.crud import get_standalone_payment, update_payment_details
from lnbits.core.models import CreateInvoice, Payment
from lnbits.core.views.admin_api import api_auditor
from lnbits.core.views.api import api_payment
from lnbits.settings import settings
from lnbits.wallets import get_wallet_class
from lnbits.core.tasks import register_websocket_listener, websocket_listeners

from ...helpers import (
    cancel_invoice,
    get_random_invoice_data,
    is_fake,
    is_regtest,
    pay_real_invoice,
    settle_invoice,
)

WALLET = get_wallet_class()

# check sending a ping message to the websocket
@pytest.mark.asyncio
async def test_websocket_ping_pong(from_wallet_ws):
    from_wallet_ws.send_text("ping")
    data = from_wallet_ws.receive_text()
    assert(data == 'pong')

# Verify that a message sent to the websocket is received in the queue
@pytest.mark.asyncio
async def test_websocket_message(from_wallet_ws,from_wallet):
    websocket_queue = asyncio.Queue(5)
    register_websocket_listener(websocket_queue,from_wallet.id)
    from_wallet_ws.send_text("some data")
    data = await websocket_queue.get()
    assert(data == 'some data')

# Verify that a message sent to the websocket is received by twi listeners
@pytest.mark.asyncio
async def test_websocket_message_multiple(from_wallet_ws,from_wallet):
    websocket_queue_one = asyncio.Queue(5)
    websocket_queue_two = asyncio.Queue(5)
    register_websocket_listener(websocket_queue_one,from_wallet.id)
    register_websocket_listener(websocket_queue_two,from_wallet.id)
    from_wallet_ws.send_text("some data")
    data = await websocket_queue_one.get()
    assert(data == 'some data')
    data = await websocket_queue_two.get()
    assert(data == 'some data')


# Verify that websocket_listener is removed from websocket_listeners
#@pytest.mark.asyncio
#async def test_websocket_listener(from_wallet_ws,from_wallet):
#    qlen = 5
#    websocket_queue = asyncio.Queue(qlen)
#    assert(len(websocket_listeners) == 0)    
#    register_websocket_listener(websocket_queue,from_wallet.id)
#    assert(len(websocket_listeners) == 1)    
#    for n in range(qlen + 1):
#        from_wallet_ws.send_text("some data")
#    await asyncio.sleep(1)
#    assert(len(websocket_listeners)==0)
