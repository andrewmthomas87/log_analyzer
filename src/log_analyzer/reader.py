# Adapted from WPILib's reference DataLog reader:
# https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/examples/printlog/datalog.py
#
# Copyright (c) FIRST and other WPILib contributors.
# Open Source Software; you can modify and/or share it under the terms of
# the WPILib BSD license file in the root directory of this project.

import array
import struct
from typing import List, SupportsBytes

floatStruct = struct.Struct("<f")
doubleStruct = struct.Struct("<d")

CONTROL_START = 0
CONTROL_FINISH = 1
CONTROL_SET_METADATA = 2


class StartRecordData:
    """Data contained in a start control record."""

    def __init__(self, entry: int, name: str, type: str, metadata: str):
        self.entry = entry
        self.name = name
        self.type = type
        self.metadata = metadata


class MetadataRecordData:
    """Data contained in a set metadata control record."""

    def __init__(self, entry: int, metadata: str):
        self.entry = entry
        self.metadata = metadata


class DataLogRecord:
    """A record in the data log. May represent either a control record
    (entry == 0) or a data record."""

    def __init__(self, entry: int, timestamp: int, data: SupportsBytes):
        self.entry = entry
        self.timestamp = timestamp
        self.data = data

    def is_control(self) -> bool:
        return self.entry == 0

    def _control_type(self) -> int:
        return self.data[0]

    def is_start(self) -> bool:
        return (
            self.entry == 0
            and len(self.data) >= 17
            and self._control_type() == CONTROL_START
        )

    def is_finish(self) -> bool:
        return (
            self.entry == 0
            and len(self.data) == 5
            and self._control_type() == CONTROL_FINISH
        )

    def is_set_metadata(self) -> bool:
        return (
            self.entry == 0
            and len(self.data) >= 9
            and self._control_type() == CONTROL_SET_METADATA
        )

    def get_start_data(self) -> StartRecordData:
        if not self.is_start():
            raise TypeError("not a start record")
        entry = int.from_bytes(self.data[1:5], byteorder="little", signed=False)
        name, pos = self._read_inner_string(5)
        type_, pos = self._read_inner_string(pos)
        metadata = self._read_inner_string(pos)[0]
        return StartRecordData(entry, name, type_, metadata)

    def get_finish_entry(self) -> int:
        if not self.is_finish():
            raise TypeError("not a finish record")
        return int.from_bytes(self.data[1:5], byteorder="little", signed=False)

    def get_set_metadata_data(self) -> MetadataRecordData:
        if not self.is_set_metadata():
            raise TypeError("not a set metadata record")
        entry = int.from_bytes(self.data[1:5], byteorder="little", signed=False)
        metadata = self._read_inner_string(5)[0]
        return MetadataRecordData(entry, metadata)

    def get_boolean(self) -> bool:
        if len(self.data) != 1:
            raise TypeError("not a boolean")
        return self.data[0] != 0

    def get_integer(self) -> int:
        if len(self.data) != 8:
            raise TypeError("not an integer")
        return int.from_bytes(self.data, byteorder="little", signed=True)

    def get_float(self) -> float:
        if len(self.data) != 4:
            raise TypeError("not a float")
        return floatStruct.unpack(self.data)[0]

    def get_double(self) -> float:
        if len(self.data) != 8:
            raise TypeError("not a double")
        return doubleStruct.unpack(self.data)[0]

    def get_string(self) -> str:
        return str(self.data, encoding="utf-8")

    def get_boolean_array(self) -> List[bool]:
        return [x != 0 for x in self.data]

    def get_integer_array(self) -> array.array:
        if (len(self.data) % 8) != 0:
            raise TypeError("not an integer array")
        arr = array.array("l")
        arr.frombytes(self.data)
        return arr

    def get_float_array(self) -> array.array:
        if (len(self.data) % 4) != 0:
            raise TypeError("not a float array")
        arr = array.array("f")
        arr.frombytes(self.data)
        return arr

    def get_double_array(self) -> array.array:
        if (len(self.data) % 8) != 0:
            raise TypeError("not a double array")
        arr = array.array("d")
        arr.frombytes(self.data)
        return arr

    def get_string_array(self) -> List[str]:
        size = int.from_bytes(self.data[:4], byteorder="little", signed=False)
        if size > ((len(self.data) - 4) / 4):
            raise TypeError("not a string array")
        arr = []
        pos = 4
        for _ in range(size):
            val, pos = self._read_inner_string(pos)
            arr.append(val)
        return arr

    def _read_inner_string(self, pos: int) -> tuple[str, int]:
        size = int.from_bytes(
            self.data[pos : pos + 4], byteorder="little", signed=False
        )
        end = pos + 4 + size
        if end > len(self.data):
            raise TypeError("invalid string size")
        return str(self.data[pos + 4 : end], encoding="utf-8"), end


class _DataLogIterator:
    """DataLogReader iterator."""

    def __init__(self, buf: SupportsBytes, pos: int):
        self.buf = buf
        self.pos = pos

    def __iter__(self):
        return self

    def _read_var_int(self, pos: int, length: int) -> int:
        val = 0
        for i in range(length):
            val |= self.buf[pos + i] << (i * 8)
        return val

    def __next__(self) -> DataLogRecord:
        if len(self.buf) < (self.pos + 4):
            raise StopIteration
        entry_len = (self.buf[self.pos] & 0x3) + 1
        size_len = ((self.buf[self.pos] >> 2) & 0x3) + 1
        timestamp_len = ((self.buf[self.pos] >> 4) & 0x7) + 1
        header_len = 1 + entry_len + size_len + timestamp_len
        if len(self.buf) < (self.pos + header_len):
            raise StopIteration
        entry = self._read_var_int(self.pos + 1, entry_len)
        size = self._read_var_int(self.pos + 1 + entry_len, size_len)
        timestamp = self._read_var_int(
            self.pos + 1 + entry_len + size_len, timestamp_len
        )
        if len(self.buf) < (self.pos + header_len + size):
            raise StopIteration
        record = DataLogRecord(
            entry,
            timestamp,
            self.buf[self.pos + header_len : self.pos + header_len + size],
        )
        self.pos += header_len + size
        return record


class DataLogReader:
    """Data log reader (reads logs written by the DataLog class)."""

    def __init__(self, buf: SupportsBytes):
        self.buf = buf

    def __bool__(self):
        return self.is_valid()

    def is_valid(self) -> bool:
        return (
            len(self.buf) >= 12
            and self.buf[:6] == b"WPILOG"
            and self.get_version() >= 0x0100
        )

    def get_version(self) -> int:
        if len(self.buf) < 12:
            return 0
        return int.from_bytes(self.buf[6:8], byteorder="little", signed=False)

    def get_extra_header(self) -> str:
        if len(self.buf) < 12:
            return ""
        size = int.from_bytes(self.buf[8:12], byteorder="little", signed=False)
        return str(self.buf[12 : 12 + size], encoding="utf-8")

    def __iter__(self) -> _DataLogIterator:
        extra_header_size = int.from_bytes(
            self.buf[8:12], byteorder="little", signed=False
        )
        return _DataLogIterator(self.buf, 12 + extra_header_size)
