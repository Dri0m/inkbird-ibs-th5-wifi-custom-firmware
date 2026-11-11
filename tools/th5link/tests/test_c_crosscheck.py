"""C (firmware/src/link_proto.h) vs Python (proto.py): same bytes both ways."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.th5link import proto as P

ROOT = Path(__file__).resolve().parents[3]
SRC = Path(__file__).with_name("c_vectors.c")

pytestmark = pytest.mark.skipif(not shutil.which("clang"), reason="clang not found")


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("c") / "c_vectors"
    subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                    "-fsanitize=address,undefined", f"-I{ROOT / 'firmware/src'}", str(SRC),
                    "-o", str(out)], check=True)
    return out


def python_vectors():
    cfg = {P.CFG_MODE: 1, P.CFG_SYNC_MIN: 60, P.CFG_CLOCK_MIN: 5, P.CFG_CLOCK_24H: 1,
           P.CFG_FULL_N: 30, P.CFG_T_OFF: -50, P.CFG_RH_OFF: 120, P.CFG_FLAGS: P.CFG_FLAG_BADGE}
    hr = P.HelloRsp(1, P.RST_POR | P.RST_BOR, P.BAUD_FAST, 1024, 0x10000, 0x1122334455667788,
                    0xDEADBEEF, 1, P.TRIG_BUTTON, P.F_TIME_VALID | P.F_FRAME_VALID | P.F_SENSOR_OK,
                    -1234, 5280, 3980, 78, P.CHG_CHARGING, 4321, P.RES_OK, 0, 1)
    E = P.encode_frame
    return {
        "crc16": f"{P.crc16(b'123456789'):04x}",
        "hello": E(0, P.HELLO, P.Hello(1, 256, 0x00010203).encode()).hex(),
        "hello_rsp": E(0, P.HELLO_RSP, hr.encode()).hex(),
        "net": E(3, P.NET, P.Net(P.NET_API_UP, 0, -58).encode()).hex(),
        "time": E(4, P.TIME, P.Time(1793520000, 60, 1806195600, 120).encode()).hex(),
        "config": E(5, P.CONFIG, P.config_encode(cfg)).hex(),
        "img_begin": E(7, P.IMG_BEGIN, P.ImgBegin(0x0123456789ABCDEF, 2, 0, 1, 24960, 0xCAFEBABE).encode()).hex(),
        "img_data": E(8, P.IMG_DATA, P.img_data_encode(P.IMG_CHUNK, bytes(range(0xF0, 0xF8)))).hex(),
        "img_end": E(9, P.IMG_END, P.img_end_encode(0x0123456789ABCDEF)).hex(),
        "hold": E(10, P.HOLD, P.hold_encode(900)).hex(),
        "bye": E(11, P.BYE, P.bye_encode(P.BYE_DONE)).hex(),
        "forget": E(0, P.FORGET).hex(),
        "fw_begin": E(12, P.FW_BEGIN, P.FwBegin(0x00020001, 32572, 0x0BADF00D).encode()).hex(),
        "fw_end": E(13, P.FW_END, P.fw_end_encode(0x0BADF00D)).hex(),
        "ack_img_data": E(8, P.ACK, P.ack_encode(P.IMG_DATA, (1028).to_bytes(4, "little"))).hex(),
        "err": E(9, P.ERR, P.Err(P.IMG_END, P.E_CRC, 0x12345678).encode()).hex(),
    }


def test_c_encodes_like_python(exe):
    out = subprocess.run([str(exe), "emit"], check=True, capture_output=True, text=True).stdout
    c = dict(line.split(" ", 1) for line in out.strip().splitlines())
    py = python_vectors()
    assert c.keys() == py.keys()
    for k in py:
        assert c[k] == py[k], k


def test_c_decodes_python_frames(exe):
    hr = P.HelloRsp(frame_id=0xFEDCBA9876543210, settings_crc=0x01020304, t_centi=-4005,
                    rh_centi=9999, batt_mv=3650, flags=0x2D, baud=P.BAUD_SLOW, boot_ver=7)
    good = [
        P.encode_frame(77, P.HELLO_RSP, hr.encode()),
        P.encode_frame(78, P.TIME, P.Time(1800000000, -300, 0, 0).encode()),
        P.encode_frame(79, P.IMG_BEGIN, P.ImgBegin(42, 1, 0, 0, 12480, 0xA5A5A5A5).encode()),
        P.encode_frame(80, P.IMG_DATA, P.img_data_encode(0, bytes(1020))),
        P.encode_frame(81, P.PING, b""),
        P.encode_frame(82, P.FW_BEGIN, P.FwBegin(0x00020001, 50144, 0xA5A5A5A5).encode()),
    ]
    bad = [
        good[1][:-1] + bytes([good[1][-1] ^ 1]),                 # CRC
        bytes([0xA5, 2]) + good[1][2:],                          # version
        P.encode_frame(1, P.IMG_DATA, bytes(1025)),              # > LINK_MAX_TO_MCU
        good[1][:-3],                                            # truncated
    ]
    stdin = "\n".join(f.hex() for f in good + bad) + "\n"
    lines = subprocess.run([str(exe), "check"], input=stdin, check=True, capture_output=True,
                           text=True).stdout.strip().splitlines()
    assert lines[0] == ("44 seq=77 cmd=129 baud=115200 frame_id=fedcba9876543210 crc=01020304 "
                        "t=-4005 rh=9999 mv=3650 flags=45 boot_ver=7")
    assert lines[1] == "12 seq=78 cmd=4 utc=1800000000 off=-300 next=0 next_off=0"
    assert lines[2] == "20 seq=79 cmd=6 id=000000000000002a bpp=1 len=12480 crc=a5a5a5a5"
    assert lines[3] == "1024 seq=80 cmd=7"
    assert lines[4] == "0 seq=81 cmd=2"
    assert lines[5] == "16 seq=82 cmd=11 ver=00020001 len=50144 crc=a5a5a5a5 load=00001000"
    assert lines[6:] == ["-1"] * 4


def test_c_mailbox_rings(exe):
    r = subprocess.run([str(exe), "mbox"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "mbox ok"


def test_header_constants_match():
    """Every LINK_* number the Python side also defines agrees."""
    import re
    h = (ROOT / "firmware/src/link_proto.h").read_text()

    def define(name):
        m = re.search(rf"#define {name}\s+\(?([0-9A-Fa-fx]+)u?", h)
        return int(m.group(1).rstrip("u"), 0)

    def enum(name):
        m = re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", h)
        return int(m.group(1), 0)
    assert define("LINK_MAX_TO_MCU") == P.MAX_TO_MCU and define("LINK_MAX_TO_CBU") == P.MAX_TO_CBU
    assert define("LINK_BAUD_FAST") == P.BAUD_FAST and define("LINK_IMG_CHUNK") == P.IMG_CHUNK
    assert define("LINK_MBOX_RING") == P.MBOX_RING and define("LINK_MBOX_MAGIC") == P.MBOX_MAGIC
    assert define("LINK_HELLO_RSP_LEN") == P.HelloRsp.LEN and define("LINK_TIME_LEN") == P.Time.LEN
    assert define("LINK_IMG_BEGIN_LEN") == P.ImgBegin.LEN
    assert define("LINK_T_ACK_MS") == P.T_ACK * 1000 and define("LINK_T_GARBAGE_MS") == P.T_GARBAGE * 1000
    assert define("LINK_T_PROBE_MS") == P.T_PROBE * 1000 and define("LINK_T_CAP_MS") == P.T_CAP * 1000
    assert define("LINK_FW_BEGIN_LEN") == P.FwBegin.LEN and define("LINK_FW_END_LEN") == P.MIN_LEN[P.FW_END]
    assert define("LINK_HR_O_BOOT_VER") == 42
    fwu = (ROOT / "firmware/src/fwu.h").read_text()

    def fdefine(name):
        m = re.search(rf"#define {name}\s+(0x[0-9A-Fa-f]+|\d+)u?", fwu)
        return int(m.group(1), 0)
    assert fdefine("FWU_MAGIC") == P.FWU_MAGIC and fdefine("FWU_BOOT_MAGIC") == P.FWU_BOOT_MAGIC
    assert fdefine("FWU_HDR_BYTES") == P.FWU_HDR_BYTES and fdefine("FWU_APP_BASE") == P.FWU_APP_BASE
    assert fdefine("FWU_MIN_IMAGE") == P.FWU_MIN_IMAGE and fdefine("FWU_INFO_OFF") == P.FWU_INFO_OFF
    assert fdefine("FWU_STAGE_END") - fdefine("FWU_STAGE") == P.FWU_PKG_MAX
    for name, val in [("LINK_CMD_HELLO", P.HELLO), ("LINK_CMD_BYE", P.BYE), ("LINK_CMD_FORGET", P.FORGET),
                      ("LINK_CMD_FW_BEGIN", P.FW_BEGIN), ("LINK_CMD_FW_END", P.FW_END),
                      ("LINK_FWS_NO_BOOT", P.FWS_NO_BOOT), ("LINK_FWS_STAGED", P.FWS_STAGED),
                      ("LINK_CMD_ERR", P.ERR), ("LINK_E_BUSY", P.E_BUSY), ("LINK_CFG_FLAGS", P.CFG_FLAGS),
                      ("LINK_NET_FAIL_WIFI", P.NET_FAIL_WIFI), ("LINK_F_FACTORY_RESET", P.F_FACTORY_RESET)]:
        assert enum(name) == val, name
