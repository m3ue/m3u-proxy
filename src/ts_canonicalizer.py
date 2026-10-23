"""Opt-in TS-188 identity adapter for a single owned normalizer input.

Use one instance for an encoder lifetime; call ``start_generation()`` BEFORE
feeding bytes from each replacement upstream response (never per HTTP chunk).
``feed(bytes) -> bytes`` buffers until a CRC-checked PAT and PMT establish a
supported program; ``finish()`` validates EOF. Errors are terminal: caller must
end the owned input, not fall back to forwarding raw bytes. No auto-detection
of generation boundaries, timestamp repair, codec conversion or DRM support.

Scope: exactly one H.264 video + AAC ADTS (0x0f) or AC-3 (0x81 / DVB 0x06)
audio program. DVB private PES must identify AC-3 via descriptor 0x6a or
registration 'AC-3'; unrecognized private PES and E-AC-3 fail closed.
The audio type, PCR role and admitted descriptors must match across generations.
Only selected elementary/PCR packets are forwarded; ancillary SI/null/unknown
PIDs are discarded, not converted. PAT/PMT are regenerated with stable program,
PMT/PCR/elementary PIDs and CRC. PES, timestamps, PCR bytes and adaptation payload
are byte-exact; only TS header PID/continuity fields change. Media CCs rebase
at generation boundaries, but retain source modulo-16 deltas within a generation:
loss gaps, duplicates and adaptation-only semantics survive, as do explicit
discontinuity flags. Loss in exact multiples of 16 is unknowable from CC alone.
Rebased CCs do NOT claim continuous clocks: FFmpeg still needs timestamp recovery.

This is deliberately fail-closed, not a general TS remuxer. 192/204-byte packets,
multi-program/multi-section tables, in-generation table changes, unknown or
PID-bearing descriptors, scrambling and codec topology changes are unsupported.
"""


class TSCanonicalizationError(ValueError):
    """Malformed or unsupported input; this adapter must not be reused."""


def _crc_entry(value):
    value <<= 24
    for _ in range(8):
        value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
    return value


_CRC_TABLE = tuple(_crc_entry(i) for i in range(256))


def crc32_mpeg(data):
    """ISO/IEC 13818-1 CRC-32 (non-reflected, init all ones, no xorout)."""
    value = 0xFFFFFFFF
    for byte in data:
        value = ((value << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(value >> 24) ^ byte]
    return value


def _table(table_id, body):
    length = len(body) + 4
    data = bytes([table_id, 0xB0 | (length >> 8), length & 255]) + body
    return data + crc32_mpeg(data).to_bytes(4, 'big')


def _pid_field(pid):
    return (0xE000 | pid).to_bytes(2, 'big')


def _pid(data, offset):
    if data[offset] & 0xE0 != 0xE0:
        raise TSCanonicalizationError('invalid reserved PID bits')
    return ((data[offset] & 31) << 8) | data[offset + 1]


def _descriptors(data):
    # These descriptors contain no embedded PIDs. Reject CA and all unreviewed
    # extension/private descriptors rather than leave stale PID references.
    allowed = {0x05, 0x0A, 0x28, 0x52, 0x6A, 0x7C}
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise TSCanonicalizationError('truncated descriptor')
        tag, size = data[offset:offset + 2]
        if tag not in allowed or offset + 2 + size > len(data):
            raise TSCanonicalizationError('unsupported/invalid descriptor (including CA)')
        payload = data[offset + 2:offset + 2 + size]
        if ((tag == 0x05 and size < 4) or (tag == 0x0A and (not size or size % 4))
                or (tag == 0x52 and size != 1) or (tag == 0x28 and size != 4)
                or (tag in {0x6A, 0x7C} and not payload)):
            raise TSCanonicalizationError('invalid descriptor size')
        offset += 2 + size
    return bytes(data)


class _Section:
    def __init__(self):
        self.data = bytearray()
        self.cc = None
        self.previous = None

    def push(self, payload, start, cc):
        if self.cc == cc and self.previous == (payload, start):
            return []  # Exact duplicated TS payload, not a section discontinuity.
        if self.data and self.cc is not None and cc != (self.cc + 1) % 16:
            raise TSCanonicalizationError('PSI continuity gap')
        self.cc, self.previous = cc, (payload, start)
        result = []
        if start:
            if not payload or payload[0] >= len(payload):
                raise TSCanonicalizationError('invalid PSI pointer')
            pointer, payload = payload[0], payload[1:]
            if self.data:
                self.data.extend(payload[:pointer])
                result.extend(self._extract())
                if self.data:
                    raise TSCanonicalizationError('unfinished PSI at pointer boundary')
            payload = payload[pointer:]
        elif not self.data:
            return []  # May join an upstream in the middle of a repeated table.
        self.data.extend(payload)
        result.extend(self._extract())
        return result

    def _extract(self):
        result = []
        while self.data:
            if self.data[0] == 255:
                if any(b != 255 for b in self.data):
                    raise TSCanonicalizationError('invalid PSI stuffing')
                self.data.clear()
                break
            if len(self.data) < 3:
                break
            length = ((self.data[1] & 15) << 8) | self.data[2]
            if self.data[1] & 0xF0 != 0xB0 or not 9 <= length <= 1021:
                raise TSCanonicalizationError('invalid PSI section length/syntax')
            size = 3 + length
            if len(self.data) < size:
                break
            section = bytes(self.data[:size])
            del self.data[:size]
            if crc32_mpeg(section) != 0:
                raise TSCanonicalizationError('PSI CRC mismatch')
            if section[5] & 0xC1 != 0xC1 or section[6:8] != b'\x00\x00':
                raise TSCanonicalizationError('only current single-section tables supported')
            result.append(section)
        return result


class TSCanonicalizer:
    """Incremental bounded TS-188 adapter; no I/O and no global mutable state."""

    def __init__(self, max_probe_bytes=1024 * 1024):
        if max_probe_bytes < 376:
            raise ValueError('max_probe_bytes must hold at least PAT + PMT')
        self.max_probe_bytes = max_probe_bytes
        self._failed = False
        self._closed = False
        self._cc = {}
        self._signature = None
        self._canonical_pat = _table(0, b'\x00\x01\xc1\x00\x00\x00\x01' + _pid_field(4096))
        self._canonical_pmt = None
        self.start_generation()

    def _check(self):
        if self._failed or self._closed:
            raise TSCanonicalizationError('adapter is failed or finished')

    def start_generation(self):
        """Discard incomplete old TS/PSI at an explicit upstream boundary.

        Output CCs and initial compatibility signature survive. No previous
        generation bytes are ever interpreted using the new PID map.
        """
        self._check()
        self._buffer = bytearray()
        self._pending = []
        self._pat_reader = _Section()
        self._pmt_reader = _Section()
        self._source_pat = self._source_pmt = None
        self._pmt_pid = self._program = None
        self._mapping = None
        self._source_cc = {}

    def feed(self, data):
        self._check()
        output = bytearray()
        try:
            # Incremental processing bounds startup memory even for huge calls.
            view = memoryview(data)
            for pos in range(0, len(view), 188 * 64):
                self._buffer.extend(view[pos:pos + 188 * 64])
                while len(self._buffer) >= 188:
                    packet = bytes(self._buffer[:188])
                    del self._buffer[:188]
                    output.extend(self._consume(packet))
            return bytes(output)
        except (TSCanonicalizationError, IndexError) as error:
            self._failed = True
            raise TSCanonicalizationError(str(error)) from error

    def finish(self):
        self._check()
        if (self._buffer or self._mapping is None or self._pat_reader.data
                or self._pmt_reader.data):
            self._failed = True
            raise TSCanonicalizationError('truncated or unestablished TS generation')
        self._closed = True
        return b''

    @staticmethod
    def _header(packet):
        if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xC0:
            raise TSCanonicalizationError('not clear, error-free TS-188')
        afc = (packet[3] >> 4) & 3
        if afc == 0:
            raise TSCanonicalizationError('reserved adaptation control')
        offset = 4
        if afc & 2:
            length = packet[4]
            offset = 5 + length
            if offset > 188 or (afc == 2 and offset != 188) or (afc == 3 and offset >= 188):
                raise TSCanonicalizationError('invalid adaptation length')
            if length and packet[5] & 0x10 and length < 7:
                raise TSCanonicalizationError('truncated PCR')
        return ((packet[1] & 31) << 8) | packet[2], offset, bool(afc & 1)

    def _consume(self, packet):
        pid, offset, payload = self._header(packet)
        was_ready = self._mapping is not None
        if not was_ready:
            self._pending.append(packet)
            if len(self._pending) * 188 > self.max_probe_bytes:
                raise TSCanonicalizationError('PAT/PMT probe limit exceeded')
        output = bytearray()
        if payload and pid in (0, self._pmt_pid):
            reader = self._pat_reader if pid == 0 else self._pmt_reader
            for section in reader.push(packet[offset:], bool(packet[1] & 0x40), packet[3] & 15):
                if pid == 0:
                    self._accept_pat(section)
                    if was_ready:
                        output.extend(self._emit_table(0, self._canonical_pat))
                else:
                    self._accept_pmt(section)
                    if was_ready:
                        output.extend(self._emit_table(4096, self._canonical_pmt))
        if not was_ready and self._mapping is not None:
            output.extend(self._emit_table(0, self._canonical_pat))
            output.extend(self._emit_table(4096, self._canonical_pmt))
            for pending in self._pending:
                output.extend(self._remap(pending))
            self._pending.clear()
        elif was_ready:
            output.extend(self._remap(packet))
        return bytes(output)

    def _accept_pat(self, table):
        if table[0] != 0 or (len(table) - 12) % 4:
            raise TSCanonicalizationError('invalid PAT')
        if self._source_pat is not None and table != self._source_pat:
            raise TSCanonicalizationError('PAT changed without start_generation')
        programs = []
        for i in range(8, len(table) - 4, 4):
            program = int.from_bytes(table[i:i + 2], 'big')
            pid = _pid(table, i + 2)
            if program:
                programs.append((program, pid))
        if len(programs) != 1 or not 32 <= programs[0][1] < 8191:
            raise TSCanonicalizationError('exactly one valid program required')
        self._program, self._pmt_pid = programs[0]
        self._source_pat = table

    def _accept_pmt(self, table):
        if len(table) < 16 or table[0] != 2 or int.from_bytes(table[3:5], 'big') != self._program:
            raise TSCanonicalizationError('PAT/PMT program mismatch')
        if self._source_pmt is not None and table != self._source_pmt:
            raise TSCanonicalizationError('PMT changed without start_generation')
        pcr = _pid(table, 8)
        end = len(table) - 4
        def info(at):
            if at + 2 > end or table[at] & 0xF0 != 0xF0:
                raise TSCanonicalizationError('invalid descriptor length')
            size = ((table[at] & 15) << 8) | table[at + 1]
            if at + 2 + size > end:
                raise TSCanonicalizationError('descriptor loop overrun')
            return _descriptors(table[at + 2:at + 2 + size]), at + 2 + size
        program_desc, offset = info(10)
        streams = []
        while offset < end:
            if offset + 5 > end:
                raise TSCanonicalizationError('truncated ES entry')
            kind, pid = table[offset], _pid(table, offset + 1)
            desc, offset = info(offset + 3)
            if kind in (6, 129):
                # DVB carries AC-3 as private PES (0x06), identified by
                # descriptor 0x6a or registration 'AC-3'. Preserve that wire
                # representation; never relabel arbitrary private PES / EAC3.
                tags, registrations = set(), set()
                at = 0
                while at < len(desc):
                    tag, size = desc[at:at + 2]
                    tags.add(tag)
                    if tag == 5:
                        registrations.add(desc[at + 2:at + 6])
                    at += 2 + size
                if registrations - {b'AC-3'}:
                    raise TSCanonicalizationError('unsupported audio registration')
                if kind == 6 and 0x6A not in tags and b'AC-3' not in registrations:
                    raise TSCanonicalizationError('private PES is not identified DVB AC3')
            streams.append((kind, pid, desc))
        video = [s for s in streams if s[0] == 27]
        audio = [s for s in streams if s[0] in (6, 15, 129)]
        if len(streams) != 2 or len(video) != 1 or len(audio) != 1:
            raise TSCanonicalizationError('only one H264 + AAC/AC3 program supported')
        v, a = video[0], audio[0]
        if (v[1] == a[1] or any(not 32 <= p < 8191 or p == self._pmt_pid for p in (v[1], a[1], pcr))):
            raise TSCanonicalizationError('reserved or conflicting PMT/PCR/ES PID')
        role = 256 if pcr == v[1] else 257 if pcr == a[1] else 258
        signature = (a[0], role, program_desc, v[2], a[2])
        if self._signature is not None and self._signature != signature:
            raise TSCanonicalizationError('incompatible generation codec/PCR role/descriptors')
        self._signature = signature
        body = b'\x00\x01\xc1\x00\x00' + _pid_field(role)
        body += (0xF000 | len(program_desc)).to_bytes(2, 'big') + program_desc
        for stream, target in ((v, 256), (a, 257)):
            body += bytes([stream[0]]) + _pid_field(target)
            body += (0xF000 | len(stream[2])).to_bytes(2, 'big') + stream[2]
        self._canonical_pmt = _table(2, body)
        self._mapping = {v[1]: 256, a[1]: 257, pcr: role}
        self._source_pmt = table

    def _next_cc(self, pid, payload):
        previous = self._cc.get(pid)
        value = 0 if previous is None else (previous + int(payload)) % 16
        self._cc[pid] = value
        return value

    def _emit_table(self, pid, table):
        data, output, first = b'\x00' + table, bytearray(), True
        while data:
            chunk, data = data[:184], data[184:]
            output.extend(bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 255,
                                 0x10 | self._next_cc(pid, True)]))
            output.extend(chunk.ljust(184, b'\xff'))
            first = False
        return bytes(output)

    def _remap(self, packet):
        pid, _, payload = self._header(packet)
        if self._mapping is None:
            raise TSCanonicalizationError('PID map not established')
        target = self._mapping.get(pid)
        if target is None:
            return b''
        source_cc = packet[3] & 15
        previous = self._source_cc.get(pid)
        if previous is None:
            cc = self._next_cc(target, payload)
        else:
            # Rebase identity, not transport history: retain gaps, wrap and
            # same-CC duplicates (including adaptation-only CC semantics).
            cc = (self._cc[target] + source_cc - previous) % 16
            self._cc[target] = cc
        self._source_cc[pid] = source_cc
        return (bytes([packet[0], (packet[1] & 0xE0) | (target >> 8), target & 255,
                       (packet[3] & 0xF0) | cc]) + packet[4:])
