# This file is part of the Trezor project.
#
# Copyright (C) 2012-2018 SatoshiLabs and contributors
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the License along with this library.
# If not, see <https://www.gnu.org/licenses/lgpl-3.0.html>.

import functools
import logging
import sys
import warnings

from . import (
    btc,
    cosi,
    device,
    ethereum,
    exceptions,
    firmware,
    lisk,
    mapping,
    messages as proto,
    misc,
    nem,
    stellar,
    tools,
)

if sys.version_info.major < 3:
    raise Exception("Trezorlib does not support Python 2 anymore.")


SCREENSHOT = False
LOG = logging.getLogger(__name__)

PinException = exceptions.PinException


def get_buttonrequest_value(code):
    # Converts integer code to its string representation of ButtonRequestType
    return [
        k
        for k in dir(proto.ButtonRequestType)
        if getattr(proto.ButtonRequestType, k) == code
    ][0]


class MovedTo:
    """Deprecation redirector for methods that were formerly part of TrezorClient"""

    def __init__(self, where):
        self.where = where
        self.name = where.__module__ + "." + where.__name__

    def _deprecated_redirect(self, client, *args, **kwargs):
        """Redirector for a deprecated method on TrezorClient"""
        warnings.warn(
            "Function has been moved to %s" % self.name,
            DeprecationWarning,
            stacklevel=2,
        )
        return self.where(client, *args, **kwargs)

    def __get__(self, instance, cls):
        if instance is None:
            return self._deprecated_redirect
        else:
            return functools.partial(self._deprecated_redirect, instance)


class BaseClient(object):
    # Implements very basic layer of sending raw protobuf
    # messages to device and getting its response back.
    def __init__(self, transport, ui, **kwargs):
        LOG.info("creating client instance for device: {}".format(transport.get_path()))
        self.transport = transport
        self.ui = ui
        super(BaseClient, self).__init__()  # *args, **kwargs)

    def close(self):
        pass

    def cancel(self):
        self._raw_write(proto.Cancel())

    @tools.session
    def call_raw(self, msg):
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612
        self._raw_write(msg)
        return self._raw_read()

    def _raw_write(self, msg):
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612
        self.transport.write(msg)

    def _raw_read(self):
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612
        return self.transport.read()

    def callback_PinMatrixRequest(self, msg):
        pin = self.ui.get_pin(msg.type)
        if not pin.isdigit():
            raise ValueError("Non-numeric PIN provided")

        resp = self.call_raw(proto.PinMatrixAck(pin=pin))
        if isinstance(resp, proto.Failure) and resp.code in (
            proto.FailureType.PinInvalid,
            proto.FailureType.PinCancelled,
            proto.FailureType.PinExpected,
        ):
            raise exceptions.PinException(resp.code, resp.message)
        else:
            return resp

    def callback_PassphraseRequest(self, msg):
        if msg.on_device:
            passphrase = None
        else:
            passphrase = self.ui.get_passphrase()
        return self.call_raw(proto.PassphraseAck(passphrase=passphrase))

    def callback_PassphraseStateRequest(self, msg):
        self.state = msg.state
        return self.call_raw(proto.PassphraseStateAck())

    def callback_ButtonRequest(self, msg):
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612
        # do this raw - send ButtonAck first, notify UI later
        self._raw_write(proto.ButtonAck())
        self.ui.button_request(msg.code)
        return self._raw_read()

    @tools.session
    def call(self, msg):
        resp = self.call_raw(msg)
        while True:
            handler_name = "callback_{}".format(resp.__class__.__name__)
            handler = getattr(self, handler_name, None)
            if handler is None:
                break
            resp = handler(resp)  # pylint: disable=E1102

        if isinstance(resp, proto.Failure):
            if resp.code == proto.FailureType.ActionCancelled:
                raise exceptions.Cancelled
            raise exceptions.TrezorException(resp.code, resp.message)

        return resp

    def register_message(self, msg):
        """Allow application to register custom protobuf message type"""
        mapping.register_message(msg)


class ProtocolMixin(object):
    VENDORS = ("bitcointrezor.com", "trezor.io")

    def __init__(self, state=None, *args, **kwargs):
        super(ProtocolMixin, self).__init__(*args, **kwargs)
        self.state = state
        self.init_device()
        self.tx_api = None

    def set_tx_api(self, tx_api):
        self.tx_api = tx_api

    def init_device(self):
        resp = self.call(proto.Initialize(state=self.state))
        if not isinstance(resp, proto.Features):
            raise exceptions.TrezorException("Unexpected initial response")
        else:
            self.features = resp
        if str(self.features.vendor) not in self.VENDORS:
            raise RuntimeError("Unsupported device")

    @staticmethod
    def expand_path(n):
        warnings.warn(
            "expand_path is deprecated, use tools.parse_path",
            DeprecationWarning,
            stacklevel=2,
        )
        return tools.parse_path(n)

    @tools.expect(proto.Success, field="message")
    def ping(
        self,
        msg,
        button_protection=False,
        pin_protection=False,
        passphrase_protection=False,
    ):
        msg = proto.Ping(
            message=msg,
            button_protection=button_protection,
            pin_protection=pin_protection,
            passphrase_protection=passphrase_protection,
        )
        return self.call(msg)

    def get_device_id(self):
        return self.features.device_id

    def _prepare_sign_tx(self, inputs, outputs):
        tx = proto.TransactionType()
        tx.inputs = inputs
        tx.outputs = outputs

        txes = {None: tx}

        for inp in inputs:
            if inp.prev_hash in txes:
                continue

            if inp.script_type in (
                proto.InputScriptType.SPENDP2SHWITNESS,
                proto.InputScriptType.SPENDWITNESS,
            ):
                continue

            if not self.tx_api:
                raise RuntimeError("TX_API not defined")

            prev_tx = self.tx_api.get_tx(inp.prev_hash.hex())
            txes[inp.prev_hash] = prev_tx

        return txes

    @tools.expect(proto.Success, field="message")
    def clear_session(self):
        return self.call(proto.ClearSession())

    # Device functionality
    wipe_device = MovedTo(device.wipe)
    recovery_device = MovedTo(device.recover)
    reset_device = MovedTo(device.reset)
    backup_device = MovedTo(device.backup)

    set_u2f_counter = MovedTo(device.set_u2f_counter)

    apply_settings = MovedTo(device.apply_settings)
    apply_flags = MovedTo(device.apply_flags)
    change_pin = MovedTo(device.change_pin)

    # Firmware functionality
    firmware_update = MovedTo(firmware.update)

    # BTC-like functionality
    get_public_node = MovedTo(btc.get_public_node)
    get_address = MovedTo(btc.get_address)
    sign_tx = MovedTo(btc.sign_tx)
    sign_message = MovedTo(btc.sign_message)
    verify_message = MovedTo(btc.verify_message)

    # CoSi functionality
    cosi_commit = MovedTo(cosi.commit)
    cosi_sign = MovedTo(cosi.sign)

    # Ethereum functionality
    ethereum_get_address = MovedTo(ethereum.get_address)
    ethereum_sign_tx = MovedTo(ethereum.sign_tx)
    ethereum_sign_message = MovedTo(ethereum.sign_message)
    ethereum_verify_message = MovedTo(ethereum.verify_message)

    # Lisk functionality
    lisk_get_address = MovedTo(lisk.get_address)
    lisk_get_public_key = MovedTo(lisk.get_public_key)
    lisk_sign_message = MovedTo(lisk.sign_message)
    lisk_verify_message = MovedTo(lisk.verify_message)
    lisk_sign_tx = MovedTo(lisk.sign_tx)

    # NEM functionality
    nem_get_address = MovedTo(nem.get_address)
    nem_sign_tx = MovedTo(nem.sign_tx)

    # Stellar functionality
    stellar_get_address = MovedTo(stellar.get_address)
    stellar_sign_transaction = MovedTo(stellar.sign_tx)

    # Miscellaneous cryptographic functionality
    get_entropy = MovedTo(misc.get_entropy)
    sign_identity = MovedTo(misc.sign_identity)
    get_ecdh_session_key = MovedTo(misc.get_ecdh_session_key)
    encrypt_keyvalue = MovedTo(misc.encrypt_keyvalue)
    decrypt_keyvalue = MovedTo(misc.decrypt_keyvalue)


class TrezorClient(ProtocolMixin, BaseClient):
    def __init__(self, transport, *args, **kwargs):
        super().__init__(transport=transport, *args, **kwargs)
