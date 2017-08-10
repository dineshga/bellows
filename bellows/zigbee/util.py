import asyncio
import functools
import logging
import os
from crccheck.crc import CrcX25
from Crypto.Cipher import AES

import bellows.types as t


LOGGER = logging.getLogger(__name__)


class ListenableMixin:
    def add_listener(self, listener):
        id_ = id(listener)
        while id_ in self._listeners:
            id_ += 1
        self._listeners[id_] = listener
        return id_

    def listener_event(self, method_name, *args):
        for listener in self._listeners.values():
            try:
                method = getattr(listener, method_name)
                method(*args)
            except Exception as e:
                LOGGER.warning("Error calling listener.%s: %s", method_name, e)


class LocalLogMixin:
    def debug(self, msg, *args):
        return self.log(logging.DEBUG, msg, *args)

    def info(self, msg, *args):
        return self.log(logging.INFO, msg, *args)

    def warn(self, msg, *args):
        return self.log(logging.WARNING, msg, *args)

    def error(self, msg, *args):
        return self.log(logging.ERROR, msg, *args)


def zha_security(controller=False):
    empty_key_data = t.EmberKeyData()
    empty_key_data.contents = t.fixed_list(16, t.uint8_t)([t.uint8_t(0)] * 16)
    zha_key = t.EmberKeyData()
    zha_key.contents = t.fixed_list(16, t.uint8_t)(
        [t.uint8_t(c) for c in b'ZigBeeAlliance09']
    )

    isc = t.EmberInitialSecurityState()
    isc.bitmask = t.uint16_t(
        t.EmberInitialSecurityBitmask.HAVE_PRECONFIGURED_KEY |
        t.EmberInitialSecurityBitmask.REQUIRE_ENCRYPTED_KEY
    )
    isc.preconfiguredKey = zha_key
    isc.networkKey = empty_key_data
    isc.networkKeySequenceNumber = t.uint8_t(0)
    isc.preconfiguredTrustCenterEui64 = t.EmberEUI64(
        [t.uint8_t(0)] * 8
    )

    if controller:
        isc.bitmask |= (
            t.EmberInitialSecurityBitmask.TRUST_CENTER_GLOBAL_LINK_KEY |
            t.EmberInitialSecurityBitmask.HAVE_NETWORK_KEY
        )
        isc.bitmask = t.uint16_t(isc.bitmask)
        random_key = t.fixed_list(16, t.uint8_t)(
            [t.uint8_t(x) for x in os.urandom(16)]
        )
        isc.networkKey = random_key
    return isc


def retry(exceptions, retries=3, delay=0.1):
    """Return a decorator to retry a function in case of failure"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal delay, retries
            while True:
                try:
                    r = yield from func(*args, **kwargs)
                    return r
                except exceptions:
                    if retries <= 1:
                        raise
                    retries -= 1
                    yield from asyncio.sleep(delay)
                    delay *= 2
        return wrapper
    return decorator


def aesMmoHashUpdate(length, result, data):
    while len(data) >= AES.block_size:
        # Encrypt
        aes = AES.new(bytes(result), AES.MODE_ECB)
        result = bytearray(aes.encrypt(bytes(data[:AES.block_size])))

        # XOR
        for i in range(AES.block_size):
            result[i] ^= bytes(data[:AES.block_size])[i]

        data = data[AES.block_size:]
        length += AES.block_size

    return (length, result)


def aesMmoHash(data):
    result_len = 0
    remainingLength = 0
    length = len(data)
    result = bytearray([0] * AES.block_size)
    temp = bytearray([0] * AES.block_size)

    if (data and length > 0):
        remainingLength = length & (AES.block_size - 1)
        if (length >= AES.block_size):
            # Mask out the lower byte since hash update will hash
            # everything except the last piece, if the last piece
            # is less than 16 bytes.
            hashedLength = (length & ~(AES.block_size - 1))
            (result_len, result) = aesMmoHashUpdate(result_len, result, data)
            data = data[hashedLength:]

    for i in range(remainingLength):
        temp[i] = data[i]

    # Per the spec, Concatenate a 1 bit followed by all zero bits
    # (previous memset() on temp[] set the rest of the bits to zero)
    temp[remainingLength] = 0x80
    result_len += remainingLength

    # If appending the bit string will push us beyond the 16-byte boundary
    # we must hash that block and append another 16-byte block.
    if ((AES.block_size - remainingLength) < 3):
        (result_len, result) = aesMmoHashUpdate(result_len, result, temp)

        # Since this extra data is due to the concatenation,
        # we remove that length. We want the length of data only
        # and not the padding.
        result_len -= AES.block_size
        temp = bytearray([0] * AES.block_size)

    bitSize = result_len * 8
    temp[AES.block_size - 2] = (bitSize >> 8) & 0xFF
    temp[AES.block_size - 1] = (bitSize) & 0xFF

    (result_len, result) = aesMmoHashUpdate(result_len, result, temp)

    key = t.EmberKeyData()
    key.contents = t.fixed_list(16, t.uint8_t)(
        [t.uint8_t(c) for c in result]
    )
    return key


def convertInstallCode(code):
    if len(code) < 10:
        return None

    real_crc = bytes([code[-1], code[-2]])
    crc = CrcX25()
    crc.process(code[:len(code) - 2])
    if real_crc != crc.finalbytes():
        return None

    return aesMmoHash(code)
