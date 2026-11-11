"""Build-local repair for LibreTiny 1.13 BK72xx Wi-Fi: 64-hex PSK key buffer.

bk_wlan_start_sta_adv_fix() copies the key into a 33-byte stack buffer; a raw
64-hex PSK overflows it. A patched copy of wlan_ui.c is compiled instead; no
installed SDK file is edited. The build fails if the source no longer matches,
or if the patched copy never replaced the SDK file in this build.
"""
from pathlib import Path

SUFFIX = "/cores/beken-72xx/base/fixups/wlan_ui.c"


def patched_wifi_source(source: str) -> str:
    old = "char key_buffer[PMK_LEN + 1];"
    if source.count(old) != 2:
        raise RuntimeError("TH5: unsupported LibreTiny WLAN source; review raw-PSK scratch repair")
    return source.replace(old, "char key_buffer[2 * PMK_LEN + 1];")


def register(env):
    platform = Path(env.PioPlatform().get_dir())
    patched_wifi_source((platform / SUFFIX.lstrip("/")).read_text())  # fail before compiling
    applied = []

    def repair(build_env, node):
        path = Path(node.srcnode().get_abspath())
        if not path.as_posix().endswith(SUFFIX):
            return node
        out = Path(build_env.subst("$BUILD_DIR")) / "th5_sdk_fix" / path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        text = patched_wifi_source(path.read_text())
        if not out.exists() or out.read_text() != text:
            out.write_text(text)
        applied.append(out)
        local = build_env.Clone()
        local.Prepend(CPPPATH=[str(path.parent)])
        return local.Object(target=str(out.with_suffix(".o")), source=str(out))

    def check(target, source, env):
        if not applied:
            raise RuntimeError("TH5: wlan_ui.c key-buffer repair was not applied; the SDK build changed")
        print(f"TH5: wlan_ui.c key-buffer repair applied ({applied[0]})")

    # A pre: script runs before LibreTiny renames PROGNAME ("program" ->
    # "raw_firmware"), so the ELF path is taken when sources are collected.
    def attach_check(build_env, node):
        if not checked:
            checked.append(build_env.subst("$BUILD_DIR/${PROGNAME}.elf"))
            # An always-built dependency of the ELF: a pre-action on the ELF
            # did not run on incremental relinks.
            env.Depends(checked[0], env.AlwaysBuild(env.Alias("th5_sdk_check", [], check)))
        return node

    checked = []
    env.AddBuildMiddleware(attach_check)
    env.AddBuildMiddleware(repair, "*/wlan_ui.c")


# Imported normally in host tests; executed as a PlatformIO pre-script on build.
if "Import" in globals():
    Import("env")  # noqa: F821
    register(env)  # noqa: F821
