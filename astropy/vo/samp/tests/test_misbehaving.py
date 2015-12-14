# Licensed under a 3-clause BSD style license - see LICENSE.rst

import pytest

from ..hub_proxy import SAMPHubProxy
from ..client import SAMPClient
from ..integrated_client import SAMPIntegratedClient
from ..hub import SAMPHubServer

# By default, tests should not use the internet.
from .. import conf

def setup_module(module):
    conf.use_internet = False


@pytest.fixture
def receiving_client_using_misbehaving_library():
    # Get a client.
    client = SAMPIntegratedClient()
    # That uses a library that doesn't do any multithreading when receiving a message.
    client.client.receive_call = lambda *args, **kwargs: client.client._receive_call(*args, **kwargs)
    # Don't do any multithreading in the client either.
    def receive_call(private_key, sender_id, msg_id, mtype, params, extra):
        # Immediately reply.
        client.reply(msg_id, {"samp.status": "samp.ok", "samp.result": {}})
        # Take action, very time consuming.
        time.sleep(5)

    client.connect()
    client.bind_receive_call("should.be.fast", receive_call)
    return client

@pytest.fixture
def sending_client_using_misbehaving_library():
    # Get a client.
    client = SAMPIntegratedClient()
    return client

@pytest.fixture
def misbehaving_hub()
    ...


q = lambda *args, **kwargs: 4
