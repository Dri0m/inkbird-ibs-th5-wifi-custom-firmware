"""Host checks for the LibreTiny raw-PSK key-buffer repair (sdk_patch.py)."""
from pathlib import Path
import importlib.util
import os
import subprocess

import pytest

COMP = Path(__file__).resolve().parents[1] / 'components/th5_link'
spec = importlib.util.spec_from_file_location('sdk_patch', COMP / 'sdk_patch.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class FakeNode:
    def __init__(self, path):
        self.path = path

    def srcnode(self):
        return self

    def get_abspath(self):
        return str(self.path)


class FakeEnv:
    def __init__(self, platform, build):
        self.platform, self.build = platform, build
        self.vars = {"PROGNAME": "program"}  # PlatformIO's value while pre: scripts run
        self.middleware, self.pre_actions = [], {}

    def PioPlatform(self):
        return type("P", (), {"get_dir": lambda _: str(self.platform)})()

    def subst(self, text):
        return text.replace("$BUILD_DIR", str(self.build)).replace("${PROGNAME}", self.vars["PROGNAME"])

    def AddBuildMiddleware(self, fn, pattern=None):
        self.middleware.append(fn)

    def Alias(self, name, sources, action):
        return action

    def AlwaysBuild(self, node):
        return node

    def Depends(self, target, dependency):
        assert "$" not in target, "target must be substituted when the check is attached"
        self.pre_actions[target] = dependency

    def Clone(self):
        return self

    def Prepend(self, **_):
        pass

    def Object(self, target, source):
        return ("object", target, source)

    def build_node(self, node):
        for fn in self.middleware:
            node = fn(self, node)
        return node


def make_env(tmp_path):
    src = tmp_path / patch.SUFFIX.lstrip("/")
    src.parent.mkdir(parents=True)
    src.write_text("char key_buffer[PMK_LEN + 1];\n" * 2)
    env = FakeEnv(tmp_path, tmp_path / "build")
    patch.register(env)
    env.vars["PROGNAME"] = "raw_firmware"  # LibreTiny's main.py, before sources are built
    return env, src


def test_build_check_runs_on_the_real_elf(tmp_path, capsys):
    env, src = make_env(tmp_path)
    env.build_node(FakeNode(tmp_path / "other.c"))
    kind, _, out = env.build_node(FakeNode(src))
    assert kind == "object" and "2 * PMK_LEN" in Path(out).read_text()
    elf = str(tmp_path / "build" / "raw_firmware.elf")
    assert list(env.pre_actions) == [elf]
    env.pre_actions[elf]([], [], env)
    assert "repair applied" in capsys.readouterr().out


def test_build_fails_unless_repair_applied(tmp_path):
    env, _ = make_env(tmp_path)
    other = FakeNode(tmp_path / "other.c")
    assert env.build_node(other) is other
    (check,) = env.pre_actions.values()
    with pytest.raises(RuntimeError):
        check([], [], env)  # wlan_ui.c never reached the middleware


def test_sdk_source_patch_fails_closed():
    with pytest.raises(RuntimeError):
        patch.patched_wifi_source('upstream changed')
    source = 'char key_buffer[PMK_LEN + 1];\n' * 2
    fixed = patch.patched_wifi_source(source)
    assert fixed.count('char key_buffer[2 * PMK_LEN + 1];') == 2
    with pytest.raises(RuntimeError):
        patch.patched_wifi_source(fixed)


def test_sdk_installed_scratch_boundaries(tmp_path):
    sdk = Path.home() / '.platformio/platforms/libretiny/cores/beken-72xx/base/fixups/wlan_ui.c'
    if not sdk.exists():
        pytest.skip('installed LibreTiny not present')
    fixed = patch.patched_wifi_source(sdk.read_text())
    # Exercise the exact patched allocation plus copy/termination from upstream.
    start = fixed.index('char key_buffer[')
    end = fixed.index("key_buffer[g_sta_param_ptr->key_len] = '\\0';", start) + len("key_buffer[g_sta_param_ptr->key_len] = '\\0';")
    body = fixed[start:end]
    source = tmp_path / 'scratch.cpp'
    source.write_text('''#include <cstring>
#include <cassert>
#define PMK_LEN 32
struct Params { char key[65]; unsigned key_len; } params, *g_sta_param_ptr=&params;
void check() {
''' + body + '''
assert(strlen(key_buffer) == params.key_len);
assert(!memcmp(key_buffer, params.key, params.key_len));
}
int main() {
for (unsigned n : {8u, 32u, 33u, 63u, 64u}) {
  memset(params.key, 'a', sizeof(params.key)); params.key_len = n; check();
}
}
'''.replace('for (unsigned n : {8u, 32u, 33u, 63u, 64u})', 'unsigned lengths[]={8,32,33,63,64}; for (unsigned n : lengths)'))
    exe = tmp_path / 'scratch'
    subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', str(source), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


@pytest.mark.skipif(os.getenv("TH5_TEST_LINKED_PSK") != "1", reason="opt-in linked ARM emulator/JIT check")
def test_linked_raw_psk_skips_hash_worker():
    """Execute the actual linked ARM cache routine, with OS/allocator stubs.

    This matters because BDK's distributed C source differs from its linked .a.
    Optional emulator dependencies: unicorn and pyelftools. Uses synthetic keys.
    """
    import struct
    unicorn = pytest.importorskip('unicorn')
    pytest.importorskip('elftools')
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
    from elftools.elf.elffile import ELFFile
    elf_path = COMP.parents[1] / '.esphome/build/th5/.pioenvs/th5/raw_firmware.elf'
    if not elf_path.exists():
        pytest.skip('compile ESPHome before checking its linked BDK')
    with elf_path.open('rb') as stream:
        elf = ELFFile(stream)
        symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
        segments = [(s['p_vaddr'], s.data()) for s in elf.iter_segments() if s['p_type'] == 'PT_LOAD']

    for key in ('a' * 64, '0123456789ABCDEF' * 4):
        emu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB)
        emu.mem_map(0, 0x1000000)
        emu.mem_map(0x10000000, 0x10000)
        for addr, data in segments:
            emu.mem_write(addr, data)
        cache, ssid, password, heap, stack, stop = 0x10000000, 0x10000100, 0x10000200, 0x10001000, 0x1000f000, 0x1000f800
        emu.mem_write(symbols['psk_cache'], struct.pack('<I', cache))
        emu.mem_write(cache, b'\0' * 64)
        emu.mem_write(ssid, b'test-network\0')
        emu.mem_write(password, key.encode() + b'\0')
        emu.mem_write(stack, b'\0' * 4)  # fifth argument psk_len = 0
        calls = []
        alloc = [heap]

        def cstring(addr):
            out = bytearray()
            while (b := bytes(emu.mem_read(addr + len(out), 1))) != b'\0':
                out.extend(b)
            return bytes(out)

        def duplicate(data):
            addr = alloc[0]
            alloc[0] += len(data) + 8
            emu.mem_write(addr, data + b'\0')
            return addr

        names = ('rtos_get_semaphore', 'rtos_set_semaphore', 'os_free', 'os_strlen',
                 'os_strcmp', 'os_memcmp', 'hexstr2bin', 'dup_binstr', 'os_strdup', 'start_wpa_psk_cal_thread')
        hooks = {symbols[name] & ~1: name for name in names}

        def hook(uc, address, _size, _user):
            if address == stop:
                uc.emu_stop()
                return
            name = hooks.get(address)
            if name is None:
                return
            calls.append(name)
            a, b, c = [uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2)]
            result = 0
            if name == 'os_strlen':
                result = len(cstring(a))
            elif name == 'hexstr2bin':
                assert c == 32, 'PSK conversion must write exactly 32 bytes'
                uc.mem_write(b, bytes.fromhex(cstring(a).decode()))
            elif name == 'dup_binstr':
                result = duplicate(bytes(uc.mem_read(a, b)))
            elif name == 'os_strdup':
                result = duplicate(cstring(a))
            elif name in ('os_strcmp', 'os_memcmp'):
                raise AssertionError('unexpected previous cache hit in cold-start test')
            elif name == 'start_wpa_psk_cal_thread':
                raise AssertionError('raw PSK must not start PBKDF2 worker')
            uc.reg_write(UC_ARM_REG_R0, result)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

        emu.hook_add(unicorn.UC_HOOK_CODE, hook)
        for reg, value in ((UC_ARM_REG_R0, ssid), (UC_ARM_REG_R1, 12), (UC_ARM_REG_R2, password),
                           (UC_ARM_REG_R3, 0), (UC_ARM_REG_SP, stack), (UC_ARM_REG_LR, stop | 1)):
            emu.reg_write(reg, value)
        emu.emu_start(symbols['wpa_psk_request'] | 1, stop, count=10000)
        assert 'hexstr2bin' in calls
        assert bytes(emu.mem_read(cache + 12, 32)) == bytes.fromhex(key)
        assert bytes(emu.mem_read(cache + 44, 1)) == b'\2'  # CACHE_COMPLETE


def test_history_registered_before_session_initial_sync():
    import ast
    tree = ast.parse((COMP / '__init__.py').read_text())
    mapping = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'TEXT_SENSORS' for target in node.targets))
    names = [key.id for key in mapping.keys]
    assert names[-1] == 'CONF_SESSION'
    assert names.index('CONF_HISTORY') < names.index('CONF_SESSION')

