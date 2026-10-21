"""Minimal Mode S / ADS-B encoders for simulation: DF17 airborne position (CPR, baro altitude)
and Mode A/C words. Not a general-purpose library."""
import math

NZ = 15
CPR_BITS = 17
CPR_SCALE = 1 << CPR_BITS


def _nl(lat):
    """Number of longitude zones (1090-WP-9-14 / DO-260 NL function)."""
    if lat == 0:
        return 59
    if abs(lat) == 87:
        return 2
    if abs(lat) > 87:
        return 1
    a = 1 - math.cos(math.pi / (2 * NZ))
    b = math.cos(math.pi / 180.0 * abs(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(1 - a / b)))


def cpr_encode(lat, lon, odd):
    i = 1 if odd else 0
    dlat = 360.0 / (4 * NZ - i)
    yz = int(math.floor(CPR_SCALE * ((lat % dlat) / dlat) + 0.5))
    rlat = dlat * (yz / CPR_SCALE + math.floor(lat / dlat))
    nl = _nl(rlat)
    dlon = 360.0 / max(nl - i, 1)
    xz = int(math.floor(CPR_SCALE * ((lon % dlon) / dlon) + 0.5))
    return yz % CPR_SCALE, xz % CPR_SCALE


def _alt12(alt_ft):
    """12-bit altitude field, Q=1 (25 ft)."""
    n = int(round((alt_ft + 1000) / 25.0))
    n = max(0, min(n, 0x7FF))
    return ((n >> 4) << 5) | (1 << 4) | (n & 0xF)


def crc24(msg_bits_bytes):
    """Mode S parity over all but the last 3 bytes."""
    poly = 0xFFF409
    crc = 0
    for byte in msg_bits_bytes:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= poly
    return crc & 0xFFFFFF


def df17_airborne_position(icao, lat, lon, alt_ft, odd, tc=11):
    yz, xz = cpr_encode(lat, lon, odd)
    me = (tc << 51) | (0 << 49) | (0 << 48) | (_alt12(alt_ft) << 36) | (0 << 35) | ((1 if odd else 0) << 34) | (yz << 17) | xz
    head = (17 << 3) | 5
    body = bytes([head]) + icao.to_bytes(3, "big") + me.to_bytes(7, "big")
    return body + crc24(body).to_bytes(3, "big")


def squawk_word(squawk):
    """'4000' -> 0x4000 (readsb hex-octal Mode A word)."""
    return int(squawk, 16)


def _id13(word):
    """hex-octal Mode A word -> 13-bit Mode S identity field (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4)."""
    v = 0
    for bit, mask in ((0x1000, 0x0010), (0x0800, 0x1000), (0x0400, 0x0020), (0x0200, 0x2000),
                      (0x0100, 0x0040), (0x0080, 0x4000), (0x0020, 0x0100), (0x0010, 0x0001),
                      (0x0008, 0x0200), (0x0004, 0x0002), (0x0002, 0x0400), (0x0001, 0x0004)):
        if word & mask:
            v |= bit
    return v


def _ac13(alt_ft):
    """13-bit altitude field, Q=1 (25 ft), M=0."""
    n = int(round((alt_ft + 1000) / 25.0))
    n = max(0, min(n, 0x7FF))
    return ((n & 0x7E0) << 2) | ((n & 0x010) << 1) | 0x10 | (n & 0x0F)


def df11_allcall(icao):
    """DF11 all-call reply, CA=5, II=0: parity = CRC (no address overlay)."""
    body = bytes([(11 << 3) | 5]) + icao.to_bytes(3, "big")
    return body + crc24(body).to_bytes(3, "big")


def df4_altitude(icao, alt_ft):
    """DF4 surveillance altitude reply, address in the parity field (AP)."""
    v = _ac13(alt_ft)
    body = bytes([4 << 3, 0, (v >> 8) & 0x1F, v & 0xFF])
    return body + (crc24(body) ^ icao).to_bytes(3, "big")


def df5_identity(icao, word):
    """DF5 surveillance identity reply: squawk, address in the parity field (AP)."""
    v = _id13(word)
    body = bytes([5 << 3, 0, v >> 8, v & 0xFF])
    return body + (crc24(body) ^ icao).to_bytes(3, "big")
